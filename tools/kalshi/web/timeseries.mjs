// Bounded, read-only historical diagnostics. No trading configuration is changed.
const ASSETS = ['BTC', 'ETH', 'SOL', 'XRP'];
const SOURCES = ['coinbase', 'cf_archive'];
const MODELS = ['zero_drift', 'estimated_drift', 'ewma'];
const INTERVALS = [60, 300, 900, 3600, 21600, 86400];
const MAX_POINTS = 2400, MAX_BODY = 6_000_000;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const numeric = (value, low = -1e100, high = 1e100) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail('Invalid time-series number.');
const integer = (value, low = 0, high = 1e8) => Number.isSafeInteger(value) && value >= low && value <= high ? value : fail('Invalid time-series count.');
const label = (value, maximum = 2000) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail('Invalid time-series label.');
const nullable = (value, low, high) => value === null ? null : numeric(value, low, high);
const timestamp = value => {
  if (typeof value !== 'string' || !/Z$|\+00:00$/.test(value) || !Number.isFinite(Date.parse(value))) fail('Expected an explicit UTC timestamp.');
  return new Date(Date.parse(value)).toISOString();
};
const close = (left, right, tolerance = 1e-8) => Math.abs(left - right) <= tolerance * Math.max(1, Math.abs(left), Math.abs(right));
const utc = seconds => new Date(seconds * 1000).toISOString();
const format = (value, maximum = 6) => value === null || value === undefined ? 'Unavailable' : Number(value).toLocaleString('en-US', {maximumFractionDigits: maximum});
const percent = (value, places = 3) => value === null || value === undefined ? 'Unavailable' : `${(value * 100).toFixed(places)}%`;
const sigmaMinute = value => value === null || value === undefined ? 'Unavailable' : `${format(value * Math.sqrt(60) * 100, 6)}% / √minute`;

export function validateTimeSeriesReport(raw, expected = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_timeseries_analysis' || raw.version !== 1
      || !['ready', 'partial', 'unavailable'].includes(raw.status) || raw.execution_eligible !== false
      || !ASSETS.includes(raw.asset) || !SOURCES.includes(raw.source)
      || raw.price_unit !== 'USD' || raw.time_unit !== 'UTC epoch seconds') fail('Unsupported historical analysis response.');
  if (!object(raw.request) || !INTERVALS.includes(raw.request.interval) || !MODELS.includes(raw.request.model)) fail('Unsupported historical analysis request.');
  const request = {start: timestamp(raw.request.start), end: timestamp(raw.request.end), interval: raw.request.interval,
    rolling_window: integer(raw.request.rolling_window, 5, 240), max_lag: integer(raw.request.max_lag, 1, 40), model: raw.request.model};
  const low = Date.parse(request.start) / 1000, high = Date.parse(request.end) / 1000;
  if (!(high > low) || high - low > 31 * 86400 + 1) fail('Historical period exceeds the supported range.');
  for (const key of ['asset', 'source']) if (expected[key] !== undefined && expected[key] !== raw[key]) fail('Historical response belongs to a different data source.');
  for (const key of ['interval', 'model', 'rolling_window', 'max_lag']) if (expected[key] !== undefined && expected[key] !== request[key]) fail('Historical response does not match the selected analysis.');
  for (const key of ['start', 'end']) if (expected[key] !== undefined && Date.parse(expected[key]) !== Date.parse(request[key])) fail('Historical response does not match the selected period.');
  if (!object(raw.coverage)) fail('Missing historical data coverage.');
  const coverage = {};
  for (const key of ['expected_points', 'observed_points', 'missing_points', 'gap_count', 'excluded_flagged_points', 'conflicting_points', 'acquisition_count'])
    coverage[key] = integer(raw.coverage[key]);
  coverage.coverage_fraction = nullable(raw.coverage.coverage_fraction, 0, 1);
  coverage.first_time = nullable(raw.coverage.first_time, 0, 1e11);
  coverage.last_time = nullable(raw.coverage.last_time, 0, 1e11);
  coverage.largest_gap_seconds = numeric(raw.coverage.largest_gap_seconds, 0, 1e11);
  coverage.acquisition_snapshot_sha256 = raw.coverage.acquisition_snapshot_sha256 ?? null;
  if (coverage.acquisition_snapshot_sha256 !== null && (typeof coverage.acquisition_snapshot_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(coverage.acquisition_snapshot_sha256))) fail('Invalid archived acquisition identity.');
  if (coverage.expected_points !== coverage.observed_points + coverage.missing_points
      || coverage.observed_points > MAX_POINTS || coverage.expected_points > MAX_POINTS
      || coverage.expected_points && (coverage.coverage_fraction === null || !close(coverage.coverage_fraction, coverage.observed_points / coverage.expected_points))) fail('Contradictory historical coverage.');
  function rows(name, mapper, maximum = MAX_POINTS) {
    if (!Array.isArray(raw[name]) || raw[name].length > maximum) fail(`Invalid ${name} history.`);
    return raw[name].map((item, index) => object(item) ? mapper(item, index) : fail(`Invalid ${name} record.`));
  }
  function timed(name, mapper) {
    let previous = -Infinity;
    return rows(name, row => {
      const time = numeric(row.time, low, high);
      if (time <= previous) fail('Historical observations are not strictly ordered.');
      previous = time; return {time, ...mapper(row)};
    });
  }
  const points = timed('points', row => ({price: numeric(row.price, Number.MIN_VALUE, 1e15)}));
  if (points.length !== coverage.observed_points
      || points.length && (points[0].time !== coverage.first_time || points.at(-1).time !== coverage.last_time)
      || !points.length && (coverage.first_time !== null || coverage.last_time !== null)) fail('Historical observations disagree with coverage.');
  const prices = new Map(points.map(point => [point.time, point.price]));
  const returns = timed('returns', row => ({value: numeric(row.value, -100, 100)}));
  for (const row of returns) {
    const current = prices.get(row.time), previous = prices.get(row.time - request.interval);
    if (!current || !previous || !close(row.value, Math.log(current / previous), 1e-7)) fail('Returns bridge a missing observation or disagree with prices.');
  }
  const rolling = timed('rolling', row => ({volatility: numeric(row.volatility, 0, 1000), drift: numeric(row.drift, -1000, 1000)}));
  if (rolling.some(row => !prices.has(row.time))) fail('Rolling diagnostics lack a corresponding price observation.');
  const distribution = rows('distribution', row => {
    const lower = numeric(row.lower, -100, 100), upper = numeric(row.upper, -100, 100);
    if (!(upper > lower)) fail('Invalid histogram interval.');
    return {lower, upper, count: integer(row.count, 0, returns.length), density: numeric(row.density, 0, 1e100)};
  }, 100);
  if (distribution.length && distribution.reduce((total, row) => total + row.count, 0) !== returns.length) fail('Return histogram does not cover the recorded returns.');
  const acf = rows('acf', row => ({lag: integer(row.lag, 1, request.max_lag), correlation: nullable(row.correlation, -1, 1), pair_count: integer(row.pair_count, 0, returns.length)}), 200);
  if (acf.some((row, index) => index && row.lag <= acf[index - 1].lag || row.correlation !== null && row.pair_count < 2)) fail('Invalid autocorrelation coverage.');
  if (!object(raw.summary)) fail('Missing descriptive summary.');
  const summary = {return_count: integer(raw.summary.return_count, 0, MAX_POINTS)};
  for (const key of ['total_log_return', 'mean_log_return', 'std_log_return', 'volatility_per_sqrt_second', 'annualized_volatility', 'skewness', 'excess_kurtosis', 'max_drawdown', 'jb_statistic'])
    summary[key] = nullable(raw.summary[key], ['std_log_return', 'volatility_per_sqrt_second', 'annualized_volatility', 'jb_statistic'].includes(key) ? 0 : -1e100, 1e100);
  if (summary.return_count !== returns.length) fail('Return count does not match the summary.');
  let model = null;
  if (raw.model !== null) {
    if (!object(raw.model) || raw.model.id !== request.model || raw.model.descriptive !== true) fail('Unsupported historical model diagnostic.');
    model = {id: raw.model.id, label: label(raw.model.label, 200), sample_count: integer(raw.model.sample_count, 0, returns.length), descriptive: true,
      drift_per_second: nullable(raw.model.drift_per_second, -1000, 1000), volatility_per_sqrt_second: nullable(raw.model.volatility_per_sqrt_second, 0, 1000),
      ewma_half_life_steps: raw.model.ewma_half_life_steps == null ? null : numeric(raw.model.ewma_half_life_steps, Number.MIN_VALUE, 1e9)};
  }
  if (!object(raw.catalog) || !Array.isArray(raw.catalog.assets) || !Array.isArray(raw.catalog.sources)
      || !Array.isArray(raw.catalog.models) || !Array.isArray(raw.catalog.intervals)) fail('Missing historical source catalog.');
  const catalog = {assets: raw.catalog.assets.map(value => ASSETS.includes(value) ? value : fail('Unsupported history asset.')),
    sources: raw.catalog.sources.map(value => {
      if (!object(value) || !SOURCES.includes(value.id) || typeof value.configured !== 'boolean') fail('Invalid history source catalog.');
      const assets = value.assets === undefined ? [] : value.assets;
      if (!Array.isArray(assets) || assets.length > ASSETS.length) fail('Invalid archived asset coverage.');
      return {id: value.id, label: label(value.label, 200), configured: value.configured, reason: label(value.reason),
        assets: assets.map(row => {
          if (!object(row) || !ASSETS.includes(row.asset)) fail('Invalid archived asset identity.');
          const item = {asset: row.asset, first_time: nullable(row.first_time, 0, 1e11), last_time: nullable(row.last_time, 0, 1e11)};
          if (item.first_time !== null && item.last_time !== null && item.first_time > item.last_time) fail('Reversed archive coverage.');
          return item;
        })};
    }), intervals: raw.catalog.intervals.map(value => INTERVALS.includes(value) ? value : fail('Invalid history interval.')),
    models: raw.catalog.models.map(value => object(value) && MODELS.includes(value.id) ? {id: value.id, label: label(value.label, 200)} : fail('Invalid history model.')),
    max_days: integer(raw.catalog.max_days, 1, 31), max_points: integer(raw.catalog.max_points, 1, MAX_POINTS)};
  if (!catalog.assets.length || !catalog.sources.length || !catalog.models.length || !catalog.intervals.length
      || new Set(catalog.assets).size !== catalog.assets.length || new Set(catalog.sources.map(row => row.id)).size !== catalog.sources.length
      || new Set(catalog.models.map(row => row.id)).size !== catalog.models.length) fail('Duplicate or empty historical catalog.');
  if (!Array.isArray(raw.warnings) || raw.warnings.length > 40) fail('Invalid historical limitations.');
  if (raw.status === 'unavailable' && points.length || raw.status === 'ready' && (!points.length || coverage.missing_points)) fail('Historical status disagrees with its coverage.');
  let console_research_fit = null;
  if (raw.console_research_fit != null) {
    const fit = raw.console_research_fit;
    if (!object(fit) || fit.asset !== raw.asset || fit.scope !== 'dashboard_research') fail('Research fit belongs to a different asset or model scope.');
    console_research_fit = {asset: fit.asset, scope: fit.scope, half_life_seconds: numeric(fit.half_life_seconds, Number.MIN_VALUE, 1e9), volatility_scale: numeric(fit.volatility_scale, Number.MIN_VALUE, 100)};
  }
  return {kind: raw.kind, version: 1, status: raw.status, reason: label(raw.reason), asset: raw.asset, source: raw.source,
    source_label: label(raw.source_label, 300), index_id: raw.index_id === null ? null : label(raw.index_id, 80),
    fetched_at: timestamp(raw.fetched_at), request, coverage, points, returns, rolling, distribution, acf, summary, model, catalog,
    warnings: raw.warnings.map(value => label(value)), console_research_fit, execution_eligible: false};
}

export function mountTimeSeries(doc = globalThis.document, win = globalThis.window, options = {}) {
  if (doc && !doc.getElementById && doc.document) { options = doc; win = options.window || globalThis.window; doc = options.document; }
  const root = doc?.getElementById?.('timeseries-root');
  if (!root || typeof doc.createElement !== 'function' || typeof doc.createElementNS !== 'function') return null;
  const fetcher = options.fetch || win?.fetch?.bind(win) || globalThis.fetch;
  const now = options.now || (() => Date.now());
  let stopped = false, sequence = 0, controller = null, report = null, last = null, lastKey = null, sourceCatalog = null;
  const elements = {}, listeners = [];
  const el = (tag, id, className, value) => { const node = doc.createElement(tag); if (id) { node.setAttribute('id', `timeseries-${id}`); elements[id] = node; } if (className) node.setAttribute('class', className); if (value !== undefined) node.textContent = value; return node; };
  const append = (parent, ...nodes) => { for (const node of nodes) parent.appendChild(node); return parent; };
  const on = (node, name, callback) => { node.addEventListener(name, callback); listeners.push(() => node.removeEventListener?.(name, callback)); };
  const set = (id, value) => { if (elements[id]) elements[id].textContent = value; };
  const selection = (name, title, values, value) => {
    const labelNode = el('label', null, 'timeseries-control', title), select = el('select', name);
    for (const [id, text] of values) { const option = el('option', null, null, text); option.value = String(id); select.appendChild(option); }
    select.value = String(value); append(labelNode, select); return labelNode;
  };
  const input = (name, title, type, value) => { const labelNode = el('label', null, 'timeseries-control', title), field = el('input', name); field.type = type; field.value = value; append(labelNode, field); return labelNode; };
  const help = el('p', 'help', 'hint', 'Historical assets and source filters are independent of the selected trading contract. Coinbase candles are a spot proxy; CF archive observations are settlement-index evidence. Sources are never combined.');
  const controls = el('form', 'controls', 'timeseries-controls');
  append(controls,
    selection('asset', 'Historical asset', ASSETS.map(value => [value, value]), ASSETS.includes(options.asset) ? options.asset : 'BTC'),
    selection('source', 'Price source', [['coinbase', 'Coinbase closed candles · proxy'], ['cf_archive', 'Recorded CF index · archive']], 'coinbase'),
    selection('period', 'Timeframe', [['3600', 'Last hour'], ['21600', 'Last 6 hours'], ['86400', 'Last 24 hours'], ['604800', 'Last 7 days'], ['2592000', 'Last 30 days'], ['custom', 'Custom UTC period']], '86400'),
    selection('interval', 'Sampling interval', [[60, '1 minute'], [300, '5 minutes'], [900, '15 minutes'], [3600, '1 hour'], [21600, '6 hours'], [86400, '1 day']], 60),
    selection('model', 'Descriptive model', [['zero_drift', 'Zero log drift'], ['estimated_drift', 'Estimated log drift'], ['ewma', 'EWMA volatility']], 'zero_drift'),
    input('rolling', 'Rolling window · observations', 'number', '20'), input('lags', 'Maximum ACF lag · intervals', 'number', '20'));
  elements.rolling.min = '5'; elements.rolling.max = '240'; elements.rolling.step = '1';
  elements.lags.min = '1'; elements.lags.max = '40'; elements.lags.step = '1';
  const custom = el('div', 'custom', 'timeseries-custom'); custom.hidden = true;
  const initialEnd = Math.floor(now() / 60000) * 60000;
  append(custom, input('start', 'Start · UTC', 'datetime-local', new Date(initialEnd - 86400000).toISOString().slice(0, 16)),
    input('end', 'End · UTC', 'datetime-local', new Date(initialEnd).toISOString().slice(0, 16)));
  elements.start.step = '60'; elements.end.step = '60';
  const run = el('button', 'run', 'quiet', 'Run analysis'); run.type = 'submit'; controls.appendChild(custom); controls.appendChild(run);
  const useArchive = el('button', 'use-archive', 'quiet', 'Use available CF archive range'); useArchive.type = 'button'; useArchive.disabled = true; controls.appendChild(useArchive);
  const status = el('p', 'status', 'chart-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const source = el('p', 'source-status', 'hint'), coverage = el('p', 'coverage', 'hint'), warnings = el('ul', 'warnings', 'timeseries-warnings');
  const methodology = el('details', 'methodology', 'workspace-optional timeseries-methodology');
  append(methodology, el('summary', null, null, 'Methods and data limitations'), warnings);
  const stats = el('dl', 'statistics', 'timeseries-statistics');
  const diagnostics = el('div', 'diagnostics', 'timeseries-diagnostics');
  append(diagnostics, el('h3', null, null, 'Selected model diagnostics'), el('p', 'model-evidence', 'hint'), el('p', 'model-values', 'hint'), el('p', 'research-fit', 'hint'),
    el('p', null, 'hint', 'These log-return diagnostics are separate from the arithmetic Gaussian settlement-average models used by the trading runners. Parameters are descriptive fits to this selected history. They do not change a running strategy, measure probability calibration, or certify a profitable edge. Settlement-average models require exact CF observations; a Coinbase proxy cannot verify them.'));
  const grid = el('div', 'charts', 'analytics-grid timeseries-chart-grid');
  const charts = [
    ['price', 'Observed asset price', 'Closed sampled USD prices; missing intervals break the line.'],
    ['returns', 'Observed log returns', '100 × log(Pt / Pt−1), using adjacent valid sampling intervals only.'],
    ['volatility', 'Rolling model volatility', 'Volatility fitted by the selected descriptive model to each complete trailing window, scaled per square-root minute.'],
    ['distribution', 'Return distribution', 'Empirical histogram of valid adjacent log returns; counts and density are descriptive.'],
    ['acf', 'Return autocorrelation', 'Lag is measured in sampling intervals. Pair counts are reported; no independence-based confidence band is implied.'],
  ];
  for (const [id, title, hint] of charts) {
    const card = el('article', null, 'panel chart-panel'); append(card, el('h3', null, null, title));
    const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('id', `timeseries-${id}-chart`); elements[`${id}-chart`] = svg;
    svg.setAttribute('viewBox', '0 0 660 270'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title);
    append(card, svg, el('p', `${id}-status`, 'chart-status', hint)); grid.appendChild(card);
  }
  append(root, help, controls, status, source, coverage, methodology, stats, diagnostics, grid);
  const clear = () => {
    report = null; last = null; lastKey = null; stats.replaceChildren(); warnings.replaceChildren();
    for (const [id, , hint] of charts) { elements[`${id}-chart`].replaceChildren(); elements[`${id}-chart`].removeAttribute('data-chart-bounds'); elements[`${id}-chart`].removeAttribute('data-chart-scope'); elements[`${id}-chart`].setAttribute('data-stale', 'false'); set(`${id}-status`, hint); }
    set('coverage', 'No historical observations loaded for this selection.'); set('source-status', ''); set('model-values', ''); set('research-fit', ''); set('model-evidence', 'Choose a period and run analysis.');
  };
  function currentRequest() {
    const asset = elements.asset.value, source = elements.source.value, model = elements.model.value, interval = Number(elements.interval.value);
    if (!ASSETS.includes(asset) || !SOURCES.includes(source) || !MODELS.includes(model) || !INTERVALS.includes(interval)) fail('Unsupported asset, source, interval or model.');
    const rolling_window = Number(elements.rolling.value), max_lag = Number(elements.lags.value);
    integer(rolling_window, 5, 240); integer(max_lag, 1, 40);
    let start, end;
    if (elements.period.value === 'custom') {
      const parse = value => {
        if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) fail('Enter valid start and end times in UTC.');
        const parsed = Date.parse(value + 'Z'); if (!Number.isFinite(parsed)) fail('Enter valid UTC dates.');
        if (new Date(parsed).toISOString().slice(0, 16) !== value.slice(0, 16)) fail('Enter valid UTC dates.');
        return parsed;
      };
      start = parse(elements.start.value); end = parse(elements.end.value);
    } else {
      const duration = Number(elements.period.value);
      if (![3600, 21600, 86400, 604800, 2592000].includes(duration)) fail('Choose a supported timeframe.');
      end = Math.floor(now() / (interval * 1000)) * interval * 1000; start = end - duration * 1000;
    }
    if (!(end > start) || end - start > 31 * 86400000) fail('Choose a positive period of at most 31 days.');
    if (end > now() + 1000) fail('Historical analysis cannot request a future period.');
    if (Math.ceil((end - start) / (interval * 1000)) > MAX_POINTS) fail('This timeframe exceeds 2,400 samples. Choose a coarser interval.');
    return {asset, source, start: new Date(start).toISOString(), end: new Date(end).toISOString(), interval, rolling_window, max_lag, model};
  }
  const ns = (svg, tag, attributes, value) => {
    const node = doc.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, item] of Object.entries(attributes || {})) node.setAttribute(key, String(item));
    if (value !== undefined) node.textContent = String(value); svg.appendChild(node); return node;
  };
  const colors = {price: '#79b8ff', returns: '#65c9bc', volatility: '#e9b96e', distribution: '#ad9bfa', acf: '#79b8ff'};
  function frame(id, value, values, xLabel, yLabel, {time = true, zero = false, domain = null, xDomain = null} = {}) {
    const svg = elements[`${id}-chart`]; svg.replaceChildren(); svg.setAttribute('data-stale', 'false');
    svg.setAttribute('data-chart-scope', `${value.asset}|${value.source}|${value.index_id || ''}|${value.coverage.acquisition_snapshot_sha256 || value.fetched_at}|${value.request.start}|${value.request.end}|${value.request.interval}|${value.request.rolling_window}|${value.request.max_lag}|${value.request.model}|${id}`);
    const bounds = {left: 72, top: 32, right: 636, bottom: 218};
    if (!values.length) {
      svg.removeAttribute('data-chart-bounds'); ns(svg, 'text', {x: 330, y: 130, 'text-anchor': 'middle', class: 'strategy-chart-empty'}, 'Insufficient recorded observations for this diagnostic.'); return null;
    }
    svg.setAttribute('data-chart-bounds', '72 32 636 218');
    let xmin = xDomain ? xDomain[0] : Math.min(...values.map(row => row.x)), xmax = xDomain ? xDomain[1] : Math.max(...values.map(row => row.x));
    if (xmin === xmax) { xmin -= time ? value.request.interval / 2 : .5; xmax += time ? value.request.interval / 2 : .5; }
    let ymin = domain ? domain[0] : Math.min(...values.map(row => row.y)), ymax = domain ? domain[1] : Math.max(...values.map(row => row.y));
    if (zero) { ymin = Math.min(0, ymin); ymax = Math.max(0, ymax); }
    if (!domain) { const pad = ymax === ymin ? Math.max(Math.abs(ymin) * .01, 1e-8) : (ymax - ymin) * .08; ymin -= pad; ymax += pad; if (id === 'volatility' || id === 'distribution') ymin = 0; }
    const x = at => bounds.left + (at - xmin) / (xmax - xmin) * (bounds.right - bounds.left);
    const y = at => bounds.bottom - (at - ymin) / (ymax - ymin) * (bounds.bottom - bounds.top);
    const xTick = at => time ? new Date(at * 1000).toISOString().slice(5, 16).replace('T', ' ') : format(at, 4);
    const tick = at => Math.abs(at) > 0 && Math.abs(at) < .0001 ? at.toExponential(2) : format(at, 4);
    for (let i = 0; i <= 4; i++) {
      const horizontal = ymin + (ymax - ymin) * i / 4, vertical = id === 'acf' ? Math.round(xmin + (xmax - xmin) * i / 4) : xmin + (xmax - xmin) * i / 4;
      ns(svg, 'line', {x1: bounds.left, x2: bounds.right, y1: y(horizontal), y2: y(horizontal), stroke: '#293b50', 'stroke-width': 1});
      ns(svg, 'text', {x: bounds.left - 8, y: y(horizontal) + 4, 'text-anchor': 'end', class: 'chart-axis'}, tick(horizontal));
      ns(svg, 'text', {x: x(vertical), y: bounds.bottom + 19, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle', class: 'chart-axis'}, xTick(vertical));
    }
    if (ymin < 0 && ymax > 0) ns(svg, 'line', {x1: bounds.left, x2: bounds.right, y1: y(0), y2: y(0), stroke: '#8295ab', 'stroke-width': 1, 'stroke-dasharray': '4 3'});
    ns(svg, 'text', {x: bounds.left, y: 17, class: 'chart-axis'}, yLabel);
    ns(svg, 'text', {x: (bounds.left + bounds.right) / 2, y: 261, 'text-anchor': 'middle', class: 'chart-axis'}, xLabel);
    return {svg, ...bounds, x, y, xmin, xmax, ymin, ymax};
  }
  function lineChart(id, value, rows, yLabel, detail) {
    const plot = frame(id, value, rows, 'Observation time · UTC', yLabel); if (!plot) return;
    const {svg, x, y} = plot; let part = [], previous = null;
    const finish = () => { if (part.length > 1) ns(svg, 'path', {d: part.map((row, index) => `${index ? 'L' : 'M'}${x(row.x).toFixed(3)},${y(row.y).toFixed(3)}`).join(' '), fill: 'none', stroke: colors[id], 'stroke-width': 1.7, 'data-chart-series': id, 'data-chart-label': yLabel}); part = []; };
    for (const row of rows) {
      if (previous !== null && row.x - previous > value.request.interval * 1.01) finish();
      part.push(row); previous = row.x;
    }
    finish();
    for (const row of rows) ns(svg, 'circle', {cx: x(row.x).toFixed(3), cy: y(row.y).toFixed(3), r: rows.length > 500 ? 1.8 : 3,
      fill: colors[id], 'data-chart-series': id, 'data-chart-label': yLabel, 'data-chart-detail': detail(row)});
  }
  function draw(value) {
    const {request, summary, coverage: evidence} = value;
    const sourceText = `${value.source_label}${value.index_id ? ` / ${value.index_id}` : ''} | ${value.asset} / USD | UTC | ${request.interval}s sampling | acquired ${value.fetched_at}`;
    set('source-status', sourceText);
    set('coverage', `Requested ${request.start} to ${request.end} | ${evidence.observed_points} / ${evidence.expected_points} expected samples (${percent(evidence.coverage_fraction, 2)}) | ${evidence.missing_points} missing | ${evidence.gap_count} missing runs; largest ${format(evidence.largest_gap_seconds, 1)} seconds${evidence.first_time === null ? '' : ` | observed ${utc(evidence.first_time)} to ${utc(evidence.last_time)}`} | ${evidence.excluded_flagged_points} flagged observations excluded; ${evidence.conflicting_points} conflicts.`);
    warnings.replaceChildren(); for (const warning of value.warnings) warnings.appendChild(el('li', null, null, warning));
    stats.replaceChildren();
    const metrics = [
      ['Observed prices', String(value.points.length)], ['Adjacent valid returns', String(summary.return_count)],
      ['Observed endpoint log return', percent(summary.total_log_return)], ['Mean log return / interval', percent(summary.mean_log_return)],
      ['Sample return deviation', percent(summary.std_log_return)], ['RMS volatility', sigmaMinute(summary.volatility_per_sqrt_second)],
      ['Skewness', format(summary.skewness, 4)], ['Excess kurtosis', format(summary.excess_kurtosis, 4)],
      ['Price drawdown', percent(summary.max_drawdown)], ['Jarque–Bera statistic', format(summary.jb_statistic, 4)],
    ];
    for (const [name, result] of metrics) { const wrapper = el('div'); append(wrapper, el('dt', null, null, name), el('dd', null, null, result)); stats.appendChild(wrapper); }
    const model = value.model;
    set('model-evidence', model ? `${model.label} | ${model.sample_count} adjacent returns | fitted from the selected historical window; descriptive analysis only.` : 'Insufficient history for the selected model diagnostic.');
    set('model-values', model ? `Log drift ${model.drift_per_second === null ? 'Unavailable' : `${format(model.drift_per_second * 3600 * 100, 6)}% / hour`} | volatility ${sigmaMinute(model.volatility_per_sqrt_second)}${model.ewma_half_life_steps === null ? '' : ` | EWMA half-life ${format(model.ewma_half_life_steps, 3)} intervals`}.` : '');
    set('research-fit', value.console_research_fit ? `Console CF research fit: ${value.console_research_fit.asset}, EWMA half-life ${format(value.console_research_fit.half_life_seconds, 3)} seconds, volatility scale ${format(value.console_research_fit.volatility_scale, 6)}. This asset-bound research fit is separate from the descriptive fit above and any standalone runner.` : 'No asset-bound research fit is loaded for this asset in this console.');
    lineChart('price', value, value.points.map(row => ({x: row.time, y: row.price})), `${value.asset} price · USD`, row => `${value.asset} | ${value.source_label} | ${utc(row.x)} | observed sampled price $${format(row.y, 8)} | ${request.interval}s interval`);
    lineChart('returns', value, value.returns.map(row => ({x: row.time, y: row.value * 100})), 'Log return · % per interval', row => `${value.asset} | ${utc(row.x)} | adjacent ${request.interval}s log return ${format(row.y, 8)}% | missing intervals are excluded`);
    lineChart('volatility', value, value.rolling.map(row => ({x: row.time, y: row.volatility * Math.sqrt(60) * 100})), 'Volatility · % / √minute', row => `${value.asset} | ${utc(row.x)} | trailing ${request.rolling_window} intervals | volatility ${format(row.y, 8)}% / √minute`);
    const histogram = value.distribution.map(row => ({...row, x: (row.lower + row.upper) * 50, y: row.count}));
    const bars = frame('distribution', value, histogram, 'Adjacent log return · %', 'Recorded return count', {time: false, zero: true,
      xDomain: histogram.length ? [histogram[0].lower * 100, histogram.at(-1).upper * 100] : null});
    if (bars) for (const row of histogram) {
      const left = bars.x(row.lower * 100), right = bars.x(row.upper * 100), width = Math.max(1, right - left - 1);
      ns(bars.svg, 'rect', {x: Math.max(bars.left, left).toFixed(3), y: bars.y(row.count).toFixed(3), width: Math.min(width, bars.right - Math.max(bars.left, left)).toFixed(3), height: Math.max(0, bars.y(0) - bars.y(row.count)).toFixed(3), fill: colors.distribution,
        'data-chart-series': 'distribution', 'data-chart-label': 'Empirical returns', 'data-chart-detail': `${format(row.lower * 100, 8)}% ≤ log return < ${format(row.upper * 100, 8)}% (last bin includes upper endpoint) | ${row.count} of ${value.returns.length} returns | density ${format(row.density, 8)} per unit log return`});
    }
    const correlations = value.acf.filter(row => row.correlation !== null).map(row => ({...row, x: row.lag, y: row.correlation}));
    const acfPlot = frame('acf', value, correlations, `Lag · ${request.interval}s intervals`, 'Return autocorrelation', {time: false, zero: true, domain: [-1, 1], xDomain: request.max_lag === 1 ? [.5, 1.5] : [1, request.max_lag]});
    if (acfPlot) for (const row of correlations) {
      const x = acfPlot.x(row.lag), y = acfPlot.y(row.correlation);
      ns(acfPlot.svg, 'line', {x1: x, x2: x, y1: acfPlot.y(0), y2: y, stroke: colors.acf, 'stroke-width': 3});
      ns(acfPlot.svg, 'circle', {cx: x, cy: y, r: 4, fill: colors.acf, 'data-chart-series': 'acf', 'data-chart-label': 'Observed return ACF',
        'data-chart-detail': `Lag ${row.lag} (${row.lag * request.interval} seconds) | return autocorrelation ${format(row.correlation, 8)} | ${row.pair_count} recorded pairs`});
    }
    set('price-status', `${value.points.length} sampled prices. ${value.source === 'coinbase' ? 'Coinbase is a proxy, not the contract settlement index.' : 'Exact native CF snapshots at the selected UTC sampling times.'} Missing observations are not interpolated.`);
    set('returns-status', `${value.returns.length} adjacent valid log returns. Gaps never create a multi-interval return labeled as a one-interval move.`);
    set('volatility-status', `${value.rolling.length} trailing ${request.rolling_window}-interval estimates from ${value.model?.label || request.model}. Zero or insufficient observations do not produce a fitted volatility.`);
    set('distribution-status', `${value.returns.length} return observations; empirical histogram. It is not a forecast or a calibrated outcome-probability distribution.`);
    set('acf-status', `${correlations.length} estimable lags of ${value.acf.length} requested. Null correlations remain unavailable; pair counts appear in inspection.`);
  }
  function catalog(value) {
    sourceCatalog = value.catalog;
    const configured = value.catalog.sources.map(item => `${item.label}: ${item.configured ? 'source enabled' : 'not configured'}${item.reason ? ` (${item.reason})` : ''}`);
    help.textContent = `Historical filters are independent of the trading contract. ${configured.join(' · ')}. Coinbase candles are a spot proxy; CF archive observations are settlement-index evidence. Sources are never combined.`;
    updateArchiveControls();
  }
  function updateArchiveControls() {
    const archived = sourceCatalog?.sources.find(item => item.id === 'cf_archive')?.assets.find(item => item.asset === elements.asset.value);
    useArchive.disabled = !archived || archived.first_time === null || archived.last_time === null || archived.last_time <= archived.first_time;
    useArchive.archiveRange = archived || null;
  }
  async function refresh() {
    if (stopped) return;
    let request;
    try { request = currentRequest(); } catch (error) { set('status', error.message); return; }
    const key = JSON.stringify(request), version = ++sequence;
    controller?.abort(); controller = typeof AbortController === 'function' ? new AbortController() : null;
    const activeController = controller;
    if (lastKey !== key) clear();
    run.disabled = true; set('status', 'Loading bounded historical observations and calculating descriptive diagnostics…');
    let timeout = null;
    try {
      if (activeController && win?.setTimeout) timeout = win.setTimeout(() => activeController.abort(), 45000);
      const parameters = new URLSearchParams(Object.entries(request).map(([name, value]) => [name, String(value)]));
      const response = await fetcher(`/api/timeseries?${parameters}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: activeController?.signal});
      if (!response.ok) fail(response.status === 404 ? 'Restart the web console to enable the historical analysis endpoint.' : `Historical analysis request failed (HTTP ${response.status}).`);
      const body = await response.text(); if (body.length > MAX_BODY) fail('Historical response exceeded its size limit.');
      const value = validateTimeSeriesReport(JSON.parse(body), request);
      if (stopped || version !== sequence) return;
      report = value; last = value; lastKey = key; catalog(value); draw(value); set('status', `${value.status === 'ready' ? 'Analysis loaded' : value.status === 'partial' ? 'Partial history' : 'History unavailable'}: ${value.reason}`);
    } catch (error) {
      if (stopped || version !== sequence) return;
      report = null;
      if (last && lastKey === key) {
        for (const [id] of charts) elements[`${id}-chart`].setAttribute('data-stale', 'true');
        set('status', `Refresh failed: ${error.message || 'History request failed'}. Retaining the prior analysis acquired ${last.fetched_at}; it is not a fresh observation.`);
      } else { clear(); set('status', error.name === 'AbortError' ? 'Historical request timed out. Run analysis again.' : error.message || 'Historical analysis is unavailable.'); }
    } finally {
      if (timeout !== null) win.clearTimeout(timeout);
      if (!stopped && version === sequence) run.disabled = false;
    }
  }
  on(controls, 'submit', event => { event.preventDefault?.(); refresh(); });
  function invalidate() { ++sequence; controller?.abort(); run.disabled = false; clear(); set('status', 'Selection changed. Run analysis to load matching historical evidence.'); }
  on(elements.period, 'change', () => {
    invalidate();
    custom.hidden = elements.period.value !== 'custom';
    if (elements.period.value !== 'custom') {
      const duration = Number(elements.period.value); const suggested = INTERVALS.find(interval => duration / interval <= MAX_POINTS);
      if (Number(elements.interval.value) < suggested) elements.interval.value = String(suggested);
    }
  });
  on(useArchive, 'click', () => {
    const archived = useArchive.archiveRange; if (!archived || useArchive.disabled) return;
    const interval = Math.min(Number(elements.interval.value), [...INTERVALS].reverse().find(item => item <= archived.last_time - archived.first_time) || 60);
    elements.interval.value = String(interval);
    const last = Math.floor(archived.last_time / 60) * 60000;
    const earliest = Math.floor(archived.first_time / 60) * 60000;
    const start = Math.max(earliest, last - Math.min(31 * 86400, MAX_POINTS * interval) * 1000);
    elements.source.value = 'cf_archive'; elements.period.value = 'custom'; custom.hidden = false;
    elements.start.value = new Date(start).toISOString().slice(0, 16); elements.end.value = new Date(last).toISOString().slice(0, 16); invalidate(); refresh();
  });
  for (const id of ['source', 'interval', 'model', 'rolling', 'lags', 'start', 'end']) on(elements[id], 'change', invalidate);
  on(elements.asset, 'change', () => setAsset(elements.asset.value));
  function setAsset(value) {
    if (stopped) return;
    if (!ASSETS.includes(value)) { ++sequence; controller?.abort(); clear(); set('status', `Historical analysis for ${String(value).slice(0, 20)} is unavailable. Choose BTC, ETH, SOL or XRP.`); return; }
    elements.asset.value = value; ++sequence; controller?.abort(); clear(); updateArchiveControls(); return refresh();
  }
  function dispose() { if (stopped) return; stopped = true; ++sequence; controller?.abort(); for (const remove of listeners) remove(); }
  win?.addEventListener?.('pagehide', dispose);
  clear(); set('status', 'Choose an asset, source and UTC period, then run analysis.');
  const ready = options.autoLoad === false ? Promise.resolve() : refresh();
  return {ready, refresh, setAsset, getReport: () => report, getLastReport: () => last, dispose};
}
