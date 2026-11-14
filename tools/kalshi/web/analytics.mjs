// Read-only charts. No probability, risk, session token or order mutation flows
// from this module into the Kelly form.
const PRODUCTS = Object.freeze({'BTC-USD': 'KXBTC15M', 'ETH-USD': 'KXETH15M', 'SOL-USD': 'KXSOL15M', 'XRP-USD': 'KXXRP15M'});
const ASSETS = new Set(Object.keys(PRODUCTS));
const symbol = product => product.replace(/-USD$/, '');
const canonical = value => value.replace(/(?:\.0+|(\.[0-9]*?)0+)$/, '$1');
const priceDigits = product => ['SOL-USD', 'XRP-USD'].includes(product) ? 4 : 2;
const priceLabel = (value, product) => Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: priceDigits(product), maximumFractionDigits: priceDigits(product)});
const MODEL_IDS = ['long_run', 'recent', 'ewma'];
const NS = 'http://www.w3.org/2000/svg';
const finite = value => typeof value === 'number' && Number.isFinite(value);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const number = (value, name, max = 1e12) => {
  if (typeof value !== 'string' || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value) || !Number.isFinite(Number(value)) || Number(value) <= 0 || Number(value) > max) fail(`Invalid ${name}.`);
  return value;
};
const iso = value => {
  if (typeof value !== 'string' || value.length > 40 || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) fail('Invalid chart timestamp.');
  return value;
};
const label = value => typeof value === 'string' && value.length <= 1000 && !/[\u0000-\u001f\u007f]/.test(value) ? value : fail('Invalid chart label.');
export function parseEnsembleWeights(values) {
  if (!object(values) || Object.keys(values).length !== MODEL_IDS.length) fail('Enter all three ensemble weights.');
  const weights = {};
  for (const id of MODEL_IDS) {
    const raw = values[id];
    if (typeof raw !== 'string' || !/^(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?$/.test(raw.trim()) || Number(raw) > 100) fail('Weights must be from 0 to 100, with at most six decimal places.');
    weights[id] = raw.trim();
  }
  if (!MODEL_IDS.some(id => Number(weights[id]) > 0)) fail('At least one model weight must be positive.');
  return weights;
}

function validateEnsemble(model, forecast) {
  forecast.components = []; forecast.paths = [];
  forecast.method = label(model.method);
  forecast.simulation_count = model.simulation_count;
  if (!Number.isSafeInteger(model.simulation_count) || model.simulation_count < 0 || model.simulation_count > 10000
      || !Array.isArray(model.components) || !Array.isArray(model.paths)) fail('Invalid ensemble diagnostics.');
  if (model.status !== 'ready') {
    if (model.components.length || model.paths.length) fail('Unavailable ensemble retains paths.');
    return;
  }
  if (model.components.length !== 3 || model.paths.length > 12 || model.simulation_count < 1) fail('Invalid ensemble size.');
  const grid = forecast.probability_fan.map(row => row.seconds);
  if (grid[0] !== 0 || forecast.price_fan.length !== grid.length || forecast.price_fan.some((row, i) => row.seconds !== grid[i])) fail('Inconsistent ensemble horizon.');
  if (['p05', 'p50', 'p95'].some(key => Math.abs(forecast.probability_fan[0][key] - forecast.probability_yes) > 1e-8)) fail('Inconsistent initial ensemble quantiles.');
  const path = rows => {
    if (!Array.isArray(rows) || rows.length !== grid.length) fail('Invalid ensemble path.');
    return rows.map((row, i) => {
      if (!object(row) || row.seconds !== grid[i] || !finite(row.probability_yes) || row.probability_yes < 0 || row.probability_yes > 1) fail('Invalid ensemble path probability.');
      return {seconds: row.seconds, probability_yes: row.probability_yes};
    });
  };
  const seen = new Set();
  forecast.components = model.components.map(component => {
    if (!object(component) || !MODEL_IDS.includes(component.id) || seen.has(component.id) || !finite(component.weight) || component.weight < 0 || component.weight > 1) fail('Invalid ensemble component.');
    seen.add(component.id);
    const disabledDegenerate = component.weight === 0 && component.probability_yes === null && component.volatility_per_sqrt_second === 0 && Array.isArray(component.probability_path) && !component.probability_path.length;
    if (!disabledDegenerate && (!finite(component.probability_yes) || component.probability_yes < 0 || component.probability_yes > 1
        || !finite(component.volatility_per_sqrt_second) || component.volatility_per_sqrt_second <= 0)) fail('Invalid ensemble component.');
    const probability_path = disabledDegenerate ? [] : path(component.probability_path);
    if (!disabledDegenerate && Math.abs(probability_path[0].probability_yes - component.probability_yes) > 1e-8) fail('Inconsistent component starting probability.');
    return {id: component.id, label: label(component.label), weight: component.weight, probability_yes: component.probability_yes,
      volatility_per_sqrt_second: component.volatility_per_sqrt_second, probability_path};
  });
  const weightSum = forecast.components.reduce((total, component) => total + component.weight, 0);
  const combined = forecast.components.reduce((total, component) => total + (component.weight === 0 ? 0 : component.weight * component.probability_yes), 0);
  if (Math.abs(weightSum - 1) > 1e-8 || Math.abs(combined - forecast.probability_yes) > 1e-8) fail('Inconsistent ensemble probability or weights.');
  const identifiers = new Set();
  forecast.paths = model.paths.map(value => {
    if (!object(value) || typeof value.id !== 'string' || !value.id.length || identifiers.has(value.id)) fail('Invalid ensemble path identity.');
    identifiers.add(value.id);
    const points = path(value.points);
    if (Math.abs(points[0].probability_yes - combined) > 1e-8) fail('Inconsistent ensemble starting probability.');
    return {id: label(value.id), points};
  });
}
export function validateMarketAnalytics(raw, expectedAsset) {
  if (!object(raw) || raw.kind !== 'kalshi_market_analytics' || raw.version !== 1 || !ASSETS.has(raw.asset) || raw.asset !== expectedAsset || !['ready', 'unavailable'].includes(raw.status)) fail('Unsupported asset analytics.');
  const result = {kind: raw.kind, version: 1, asset: raw.asset, source: label(raw.source), status: raw.status,
    fetched_at: raw.fetched_at === null ? null : iso(raw.fetched_at), error: raw.error === null ? null : label(raw.error),
    history_error: raw.history_error == null ? null : label(raw.history_error), spot: null, forecast: null};
  if (raw.status === 'ready') {
    if (!object(raw.spot) || result.fetched_at === null || !Array.isArray(raw.spot.candles) || raw.spot.candles.length > 300) fail('Incomplete spot analytics.');
    const candles = raw.spot.candles.map(row => {
      if (!object(row)) fail('Invalid candle.');
      return {time: iso(row.time), close: number(row.close, 'candle price')};
    });
    for (let i = 1; i < candles.length; i++) if (Date.parse(candles[i].time) <= Date.parse(candles[i - 1].time)) fail('Unordered candles.');
    result.spot = {price: number(raw.spot.price, 'spot price'), exchange_time: iso(raw.spot.exchange_time), candles};
  } else if (raw.spot !== null || raw.fetched_at !== null) fail('Unavailable analytics retains live data.');
  if (raw.forecast !== null && raw.forecast !== undefined) {
    const model = raw.forecast;
    if (!object(model) || !['ready', 'unavailable'].includes(model.status) || model.execution_eligible !== false || !['zero_log_drift_diffusion', 'ensemble_log_diffusion'].includes(model.model)) fail('Unsupported research model.');
    result.forecast = {status: model.status, model: model.model, execution_eligible: false, reason: label(model.reason),
      contract_ticker: model.contract_ticker === null ? null : label(model.contract_ticker), quote_id: model.quote_id === null ? null : label(model.quote_id),
      target_price: model.target_price === null ? null : number(model.target_price, 'scenario target'), close_time: model.close_time === null ? null : iso(model.close_time),
      sample_count: model.sample_count, volatility_per_sqrt_second: model.volatility_per_sqrt_second,
      probability_yes: model.probability_yes, probability_fan: [], price_fan: [], assumptions: model.assumptions};
    if (!Number.isSafeInteger(model.sample_count) || model.sample_count < 0 || model.sample_count > 300 || !Array.isArray(model.assumptions) || model.assumptions.length > 20) fail('Invalid model diagnostics.');
    model.assumptions.forEach(label);
    if (model.status === 'ready') {
      if (raw.status !== 'ready' || !finite(model.probability_yes) || model.probability_yes < 0 || model.probability_yes > 1 || !finite(model.volatility_per_sqrt_second) || model.volatility_per_sqrt_second <= 0 || typeof model.contract_ticker !== 'string' || !model.contract_ticker.startsWith(PRODUCTS[raw.asset] + '-') || !/^[a-f0-9]{64}$/.test(model.quote_id) || model.target_price === null || model.close_time === null) fail('Invalid ready model.');
      for (const key of ['probability_fan', 'price_fan']) {
        if (!Array.isArray(model[key]) || model[key].length < 2 || model[key].length > 301) fail('Invalid model trajectory.');
        result.forecast[key] = model[key].map((row, index) => {
          if (!object(row) || !finite(row.seconds) || row.seconds < 0 || row.seconds > 900 || (index && row.seconds <= model[key][index - 1].seconds) || !['p05', 'p50', 'p95'].every(k => finite(row[k])) || row.p05 > row.p50 || row.p50 > row.p95 || row.p05 < 0 || (key === 'probability_fan' && row.p95 > 1)) fail('Invalid scenario quantiles.');
          return {seconds: row.seconds, p05: row.p05, p50: row.p50, p95: row.p95};
        });
      }
    } else if (model.probability_yes !== null || model.probability_fan.length || model.price_fan.length) fail('Unavailable model retains a scenario.');
    if (model.model === 'ensemble_log_diffusion') validateEnsemble(model, result.forecast);
  }
  return result;
}

export function linePath(points, {xmin, xmax, ymin, ymax}, width = 660, height = 210, {left = 52, right = 18, top = 16, bottom = 28} = {}) {
  if (!Array.isArray(points) || ![xmin, xmax, ymin, ymax, width, height].every(finite) || xmax <= xmin || ymax <= ymin || width <= 0 || height <= 0) fail('Invalid chart domain.');
  return points.map((point, i) => {
    if (!Array.isArray(point) || point.length !== 2 || !point.every(finite)) fail('Invalid chart point.');
    return `${i ? 'L' : 'M'}${(left + (point[0] - xmin) / (xmax - xmin) * (width - left - right)).toFixed(2)},${(height - bottom - (point[1] - ymin) / (ymax - ymin) * (height - top - bottom)).toFixed(2)}`;
  }).join(' ');
}

export function cumulativeDepth(orderbook) {
  if (!object(orderbook)) fail('Missing order book.');
  const side = (rows, descending) => {
    if (!Array.isArray(rows) || rows.length > 1000) fail('Invalid book depth.');
    const parsed = rows.map(row => {
      if (!Array.isArray(row) || row.length !== 2) fail('Invalid book level.');
      const price = Number(number(row[0], 'binary price', 1));
      if (price >= 1) fail('Invalid binary price.');
      return [price, Number(number(row[1], 'book quantity', 1e8))];
    }).sort((a, b) => descending ? b[0] - a[0] : a[0] - b[0]);
    for (let i = 1; i < parsed.length; i++) if (parsed[i][0] === parsed[i - 1][0]) fail('Duplicate book price.');
    let cumulative = 0;
    return parsed.map(([price, size]) => [price, cumulative += size]);
  };
  return {bids: side(orderbook.yes_bids, true), asks: side(orderbook.yes_asks, false)};
}

// Missing observations stay visible as gaps, rather than interpolated paths.
export function chartSegments(points, maxGap) {
  if (!Array.isArray(points) || !finite(maxGap) || maxGap <= 0) fail('Invalid observation gap.');
  const segments = [];
  for (const point of points) {
    if (!Array.isArray(point) || point.length !== 2 || !point.every(finite)) fail('Invalid chart point.');
    const previous = segments.at(-1)?.at(-1);
    if (!previous || point[0] - previous[0] > maxGap) segments.push([]);
    else if (point[0] <= previous[0]) fail('Unordered chart observations.');
    segments.at(-1).push(point);
  }
  return segments;
}

function draw(doc, svg, lines, {probability = false, target = null, timeAxis = false, suffix = '', maxGap = null, zeroBaseline = false, product = 'BTC-USD', xLabel = '', yLabel = '', binaryPriceAxis = false} = {}) {
  svg.replaceChildren();
  svg.setAttribute('viewBox', '0 0 660 240');
  const points = lines.flatMap(line => line.points);
  if (!points.length) return;
  let xmin = Math.min(...points.map(p => p[0])), xmax = Math.max(...points.map(p => p[0]));
  let ymin = probability ? 0 : Math.min(...points.map(p => p[1])), ymax = probability ? 1 : Math.max(...points.map(p => p[1]));
  if (target !== null) { ymin = Math.min(ymin, target); ymax = Math.max(ymax, target); }
  if (xmin === xmax) xmax = xmin + (timeAxis ? 1000 : 1);
  if (zeroBaseline) ymin = 0;
  if (!probability) { const pad = Math.max((ymax - ymin) * .12, Math.abs(ymax) * .00001, .00001); ymin = zeroBaseline ? 0 : ymin - pad; ymax += pad; }
  const domain = {xmin, xmax, ymin, ymax};
  const size = {left: 78, right: 20, top: 25, bottom: binaryPriceAxis ? 58 : 42};
  const xAt = value => size.left + (value - xmin) / (xmax - xmin) * (660 - size.left - size.right);
  const yAt = value => 240 - size.bottom - (value - ymin) / (ymax - ymin) * (240 - size.top - size.bottom);
  const add = (tag, fields, content = '') => {
    const node = doc.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(fields)) node.setAttribute(key, String(value));
    node.textContent = content; svg.appendChild(node); return node;
  };
  const bands = lines.filter(line => line.className === 'chart-band');
  if (bands.length === 2 && bands[0].points.length === bands[1].points.length && bands[0].points.every((point, index) => point[0] === bands[1].points[index][0])) {
    add('polygon', {points: [...bands[0].points, ...bands[1].points.toReversed()].map(([x, y]) => `${xAt(x)},${yAt(y)}`).join(' '), class: 'chart-ribbon'});
  }
  for (const fraction of [0, .25, .5, .75, 1]) {
    const value = ymin + fraction * (ymax - ymin), y = yAt(value);
    add('line', {x1: size.left, x2: 640, y1: y, y2: y, class: 'chart-grid'});
    const digits = zeroBaseline ? 0 : priceDigits(product);
    add('text', {x: size.left - 10, y: y + 4, 'text-anchor': 'end', class: 'chart-label'}, probability ? `${(value * 100).toFixed(0)}%` : value.toLocaleString('en-US', {maximumFractionDigits: digits}));
  }
  for (const fraction of timeAxis && xmax - xmin < 2000 ? [0, 1] : [0, .5, 1]) {
    const x = xmin + (xmax - xmin) * fraction;
    const caption = timeAxis ? new Date(x).toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', ...(xmax - xmin <= 120000 ? {second: '2-digit'} : {})})
      : binaryPriceAxis ? `${(x * 100).toFixed(1).replace(/\.0$/, '')}¢` : `${x.toFixed(suffix === 's' ? 0 : 2)}${suffix}`;
    add('text', {x: xAt(x), y: binaryPriceAxis ? 208 : 219, 'text-anchor': fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle', class: 'chart-label'}, caption);
  }
  add('text', {x: size.left, y: 13, class: 'chart-axis-title'}, yLabel || (probability ? 'Probability of YES (%)' : zeroBaseline ? 'Cumulative contracts' : 'Underlying price (USD)'));
  add('text', {x: binaryPriceAxis ? (size.left + 660 - size.right) / 2 : 360, y: binaryPriceAxis ? 233 : 236, 'text-anchor': 'middle', class: 'chart-axis-title'}, xLabel || (timeAxis ? 'Time (local)' : suffix === 's' ? 'Seconds ahead' : ''));
  if (target !== null) {
    add('path', {d: linePath([[xmin, target], [xmax, target]], domain, 660, 240, size), class: 'chart-target'});
    add('text', {x: 634, y: Math.max(size.top + 13, Math.min(192, yAt(target) - 7)), 'text-anchor': 'end', class: 'chart-target-label'}, `Target ${priceLabel(target, product)}`);
  }
  for (const line of lines) {
    const segments = maxGap === null ? [line.points] : chartSegments(line.points, maxGap);
    for (const segment of segments) {
      const points = line.step && segment.length ? [[segment[0][0], 0], ...segment.flatMap((point, index) => index ? [[point[0], segment[index - 1][1]], point] : [point])] : segment;
      add('path', {d: linePath(points, domain, 660, 240, size), class: `chart-line ${line.className || ''}`});
      if (segment.length === 1) {
        const [x, y] = segment[0];
        add('circle', {cx: xAt(x), cy: yAt(y), r: 3, class: `chart-point ${line.className || ''}`});
      }
    }
  }
}

export function mountAnalytics(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 5000, onAssetChange = () => {}} = {}) {
  if (!doc.getElementById('analytics-panel') || typeof doc.createElementNS !== 'function') return null;
  const $ = id => doc.getElementById(id), set = (id, value) => { if ($(id)) $(id).textContent = value; };
  const ensembleEnabled = $('ensemble-controls')?.dataset?.ensemble === 'true';
  let weights = ensembleEnabled ? {long_run: '1', recent: '1', ewma: '1'} : null;
  let stopped = false, generation = 0, fetching = false, controller = null, marketState = null, latest = null, lastContract = null;
  // These snapshots belong only to the view. Freshness predicates continue to
  // use the current responses, never a retained display snapshot.
  let displayedSpot = null, displayedForecast = null, displayedMarket = null, displayedMetrics = null;
  const shownAt = {spot: null, scenario: null, depth: null, history: null};
  const shownText = {spot: '', scenario: '', depth: '', history: ''};
  // Retain actual observed trades when one-minute history is unavailable.
  // Asset changes and temporary feed failures do not mix or invent prices.
  const observations = new Map();
  let selected = $('asset-select').value || 'BTC-USD';
  if (!ASSETS.has(selected)) selected = 'BTC-USD';
  function updateLabels() {
    const asset = symbol(selected);
    set('spot-chart-title', `${asset} / USD price`);
    set('depth-chart-title', `${asset} order-book depth`);
    set('history-chart-title', `${asset} market-implied probability`);
    set('scenario-chart-title', `${asset} research probability paths`);
    set('depth-contract-label', `${asset} contract`);
    set('ensemble-caption', `Research model comparison for the active ${asset} contract`);
    for (const [id, description] of Object.entries({'spot-chart': `${asset} underlying price in USD with its current settlement target`, 'depth-chart': `${asset} cumulative Kalshi order-book depth against YES price in cents`, 'probability-chart': `${asset} observed YES bid, ask and midpoint probabilities`, 'scenario-chart': `${asset} research terminal-spot probability paths and model quantiles`})) $(id)?.setAttribute('aria-label', description);
  }
  function clearMarketCharts(message = `Loading the selected ${symbol(selected)} contract.`) {
    shownAt.depth = null; shownAt.history = null; shownText.depth = ''; shownText.history = '';
    displayedMarket = null; displayedMetrics = null;
    $('depth-chart').replaceChildren(); $('probability-chart').replaceChildren();
    tag(['depth-chart', 'probability-chart'], false);
    set('depth-status', message); set('history-status', message);
    set('rollover-status', `Watching for the next verified ${symbol(selected)} 15-minute window.`);
  }
  function clearMembers() {
    if (ensembleEnabled) $('ensemble-members').replaceChildren();
  }
  function tag(ids, stale) {
    for (const id of ids) $(id)?.setAttribute('data-stale', stale ? 'true' : 'false');
  }
  function retained(status, kind, message, ids) {
    const time = shownAt[kind];
    tag(ids, time !== null);
    set(status, time === null ? message
      : `Last known / stale | ${new Date(time).toLocaleTimeString()} | ${shownText[kind]} | ${message}`);
  }
  function show(status, kind, message) {
    shownText[kind] = message; set(status, message);
  }
  function clearScenario(message = 'Scenario unavailable.') {
    displayedForecast = null; shownAt.scenario = null; shownText.scenario = '';
    $('scenario-chart').replaceChildren(); set('scenario-value', '-');
    set('scenario-status', message); clearMembers();
    tag(['scenario-chart', 'scenario-value', 'ensemble-members'], false);
  }
  function clearSpot(message) {
    displayedSpot = null; shownAt.spot = null; shownText.spot = '';
    latest = null; set('spot-value', '-'); set('spot-status', message);
    $('spot-chart').replaceChildren(); tag(['spot-chart', 'spot-value'], false);
    clearScenario();
  }
  function retainSpot(message) {
    latest = null;
    retained('spot-status', 'spot', message, ['spot-chart', 'spot-value']);
    retained('scenario-status', 'scenario', 'Waiting for a replacement model snapshot.', ['scenario-chart', 'scenario-value', 'ensemble-members']);
  }
  function currentMarket() {
    const age = marketState?.fetched_at ? now() - Date.parse(marketState.fetched_at) : Infinity;
    const market = marketState?.market;
    return marketState?.market_status === 'ready' && market && typeof market.ticker === 'string' && market.ticker.startsWith(PRODUCTS[selected] + '-')
      && (!marketState.selected_asset || marketState.selected_asset === symbol(selected)) && age >= -1000 && age <= 5000
      && now() >= Date.parse(market.open_time) && now() < Date.parse(market.close_time) ? market : null;
  }
  function matching(model) {
    const market = currentMarket();
    return market && model?.status === 'ready' && Date.parse(market.close_time) - now() > 60000
      && market.quote_id === model.quote_id && market.ticker === model.contract_ticker
      && canonical(market.target_price) === canonical(model.target_price) && Date.parse(market.close_time) === Date.parse(model.close_time);
  }
  function renderSpot() {
    if (!displayedSpot?.spot) return;
    const value = displayedSpot;
    const points = value.spot.candles.map(row => [Date.parse(row.time) + 60000, Number(row.close)]);
    for (const tick of observations.get(value.asset) || []) {
      if (points.at(-1)?.[0] === tick[0]) points[points.length - 1] = tick;
      else if (!points.length || tick[0] > points.at(-1)[0]) points.push(tick);
    }
    const target = displayedMarket ? Number(displayedMarket.market.target_price) : null;
    const lines = [{points, className: 'chart-blue'}];
    if (displayedForecast && displayedMarket?.market.quote_id === displayedForecast.model.quote_id) {
      const model = displayedForecast.model;
      const modelTime = Date.parse(model.close_time) - model.price_fan.at(-1).seconds * 1000;
      for (const key of ['p05', 'p50', 'p95']) lines.push({points: model.price_fan.map(row => [modelTime + row.seconds * 1000, row[key]]), className: key === 'p50' ? 'chart-projection' : 'chart-band'});
    }
    draw(doc, $('spot-chart'), lines, {target, timeAxis: true, maxGap: 90000, product: selected});
    const history = value.retained_history_at
      ? `Minute history last known / stale since ${new Date(value.retained_history_at).toLocaleTimeString()}; observed live trades continue`
      : value.history_error || !value.spot.candles.length
      ? 'Minute history unavailable; collecting live trades since this page opened.'
      : '1-minute closes plus observed live trades';
    const staleSpot = !latest || now() - Date.parse(value.fetched_at) > 15000;
    const staleTarget = displayedMarket && !currentMarket();
    const staleProjection = displayedForecast && (!matching(latest?.forecast) || now() - Date.parse(displayedForecast.fetched_at) > 15000);
    tag(['spot-value'], staleSpot);
    tag(['spot-chart'], staleSpot || staleTarget || staleProjection || Boolean(value.retained_history_at));
    const detail = `${symbol(selected)} | ${value.source} | ${new Date(value.spot.exchange_time).toLocaleTimeString()} | ${history}${target === null ? '' : ` | ${staleTarget ? 'Last known target' : 'Target'} ${priceLabel(target, selected)} (${displayedMarket.market.ticker})`}${staleProjection ? ` | Last known / stale projection ${new Date(displayedForecast.fetched_at).toLocaleTimeString()}` : ''}`;
    if (!staleSpot) shownText.spot = detail;
    set('spot-status', `${staleSpot ? 'Last known / stale | ' : ''}${detail}`);
  }

  function recordTicker(value) {
    const points = observations.get(value.asset) || [];
    const current = [Date.parse(value.spot.exchange_time), Number(value.spot.price)];
    if (points.at(-1)?.[0] === current[0]) points[points.length - 1] = current;
    else if (!points.length || current[0] > points.at(-1)[0]) points.push(current);
    observations.set(value.asset, points.slice(-720));
  }
  function renderScenario() {
    const model = latest?.forecast;
    if (!matching(model) || !latest || now() - Date.parse(latest.fetched_at) > 15000 || now() >= Date.parse(model.close_time)) {
      retained('scenario-status', 'scenario', model?.status === 'unavailable' ? model.reason : `Waiting for a fresh scenario matched to the active ${symbol(selected)} contract.`, ['scenario-chart', 'scenario-value', 'ensemble-members']); return;
    }
    if (Date.parse(model.close_time) - now() <= 60000) { retained('scenario-status', 'scenario', 'Final settlement minute: waiting for a replacement settlement estimate.', ['scenario-chart', 'scenario-value', 'ensemble-members']); return; }
    displayedForecast = {model, fetched_at: latest.fetched_at}; shownAt.scenario = Date.parse(latest.fetched_at);
    $('scenario-chart').replaceChildren(); clearMembers();
    tag(['scenario-chart', 'scenario-value', 'ensemble-members'], false);
    set('scenario-value', `${(model.probability_yes * 100).toFixed(1)}%`);
    const isEnsemble = model.model === 'ensemble_log_diffusion';
    show('scenario-status', 'scenario', isEnsemble
      ? `${model.components.length} models; ${model.simulation_count} simulated trajectories; 5th / 50th / 95th mixture percentiles. Current probability uses normalized starting weights; future paths update model weights from each simulated price path. Not a calibrated settlement forecast.`
      : `Terminal spot above target scenario. ${model.sample_count} closed returns; 5th / 50th / 95th percentiles. Not a calibrated settlement forecast.`);
    const lines = [];
    if (isEnsemble && ensembleEnabled) {
      if ($('ensemble-show-paths').checked) for (const path of model.paths) lines.push({points: path.points.map(row => [row.seconds, row.probability_yes]), className: 'chart-sample'});
      if ($('ensemble-show-components').checked) for (const component of model.components) if (component.probability_path.length) lines.push({points: component.probability_path.map(row => [row.seconds, row.probability_yes]), className: `chart-component-${component.id}`});
      const make = tag => typeof doc.createElement === 'function' ? doc.createElement(tag) : doc.createElementNS('http://www.w3.org/1999/xhtml', tag);
      for (const component of model.components) {
        const row = make('tr');
        for (const value of [component.label, `${(component.weight * 100).toFixed(1)}%`, component.probability_yes === null ? 'Unavailable (excluded)' : `${(component.probability_yes * 100).toFixed(1)}%`, component.volatility_per_sqrt_second === 0 ? 'No variation' : `${(component.volatility_per_sqrt_second * Math.sqrt(60) * 100).toFixed(4)}%`]) {
          const cell = make('td'); cell.textContent = value; row.appendChild(cell);
        }
        $('ensemble-members').appendChild(row);
      }
    }
    lines.push(...['p05', 'p95', 'p50'].map(key => ({points: model.probability_fan.map(row => [row.seconds, row[key]]), className: key === 'p50' ? 'chart-blue' : 'chart-band'})));
    draw(doc, $('scenario-chart'), lines, {probability: true, suffix: 's', product: selected});
  }
  async function refresh() {
    if (stopped || fetching) return;
    const expected = ++generation, asset = selected;
    fetching = true; controller = new AbortController(); const activeController = controller;
    const timeout = win.setTimeout(() => activeController.abort(), 12000);
    try {
      const query = weights ? MODEL_IDS.map(id => `&w_${id}=${weights[id]}`).join('') : '';
      const response = await fetchImpl(`/api/analytics?asset=${asset}${query}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: activeController.signal});
      if (!response.ok) {
        if (response.status === 404) fail('The running Kalshi service has no chart endpoint. Restart the local Kalshi service, then refresh this page.');
        fail('Spot data unavailable.');
      }
      const raw = await response.text(); if (raw.length > 400000) fail('Analytics response is too large.');
      const value = validateMarketAnalytics(JSON.parse(raw), asset);
      if (stopped || expected !== generation || selected !== asset) return;
      if (weights && value.forecast?.status === 'ready' && value.forecast.model === 'ensemble_log_diffusion') {
        const total = MODEL_IDS.reduce((sum, id) => sum + Number(weights[id]), 0);
        if (value.forecast.components.some(component => Math.abs(component.weight - Number(weights[component.id]) / total) > 1e-8)) fail('The returned ensemble weights differ from the applied weights.');
      }
      if (value.status !== 'ready') { retainSpot(value.error || 'Spot feed unavailable.'); return; }
      const age = now() - Date.parse(value.fetched_at), exchangeAge = now() - Date.parse(value.spot.exchange_time);
      if (age < -1000 || age > 15000 || exchangeAge < -1000 || exchangeAge > 60000) fail('Spot data is stale.');
      recordTicker(value);
      latest = value;
      const keepHistory = !value.spot.candles.length && displayedSpot?.spot.candles.length;
      displayedSpot = {...value, spot: {...value.spot, candles: keepHistory ? displayedSpot.spot.candles : value.spot.candles},
        retained_history_at: keepHistory ? displayedSpot.retained_history_at || displayedSpot.fetched_at : null};
      shownAt.spot = Date.parse(value.spot.exchange_time);
      renderScenario();
      set('spot-value', priceLabel(value.spot.price, selected));
      renderSpot();
    } catch (error) {
      if (!stopped && expected === generation) retainSpot(error.message || 'Spot feed unavailable.');
    } finally { win.clearTimeout(timeout); if (expected === generation) { fetching = false; controller = null; } }
  }
  function setMarketState(value) {
    marketState = value;
    const incoming = value?.market_status === 'ready' && value.market?.ticker?.startsWith(PRODUCTS[selected] + '-')
      && (!value.selected_asset || value.selected_asset === symbol(selected)) ? value.market : null;
    const identity = incoming ? [incoming.ticker, incoming.quote_id, canonical(incoming.target_price), incoming.open_time, incoming.close_time].join('|') : null;
    const changed = identity && lastContract && identity !== lastContract;
    if (changed) {
      clearMarketCharts('Loading the new contract snapshot.');
      clearScenario('Waiting for the new contract model.');
    }
    if (identity) lastContract = identity;
    const fresh = currentMarket();
    if (!fresh) {
      retained('depth-status', 'depth', 'Fresh Kalshi book unavailable.', ['depth-chart']);
      retained('history-status', 'history', 'Waiting for current contract observations.', ['probability-chart']);
      if ($('rollover-status')) set('rollover-status', `Watching for the next verified active ${symbol(selected)} window. Market discovery retries automatically every 2 seconds.`);
      renderScenario(); renderSpot(); return;
    }
    displayedMarket = {market: value.market, fetched_at: value.fetched_at};
    if ($('rollover-status')) set('rollover-status', changed
      ? `New ${symbol(selected)} window loaded. Contract history and the previous window's estimate were reset; spot history continues.`
      : `Watching the active ${symbol(selected)} window. The next verified contract loads automatically when it opens.`);
    if (value.market.orderbook) {
      const depth = cumulativeDepth(value.market.orderbook);
      draw(doc, $('depth-chart'), [{points: depth.bids, className: 'chart-green', step: true}, {points: depth.asks, className: 'chart-red', step: true}], {zeroBaseline: true, binaryPriceAxis: true, xLabel: 'YES contract price (¢)', product: selected});
      const metric = value.market.metrics;
      if (metric) displayedMetrics = {value: metric, at: Date.parse(value.fetched_at)};
      const shownMetric = displayedMetrics?.value;
      shownAt.depth = Date.parse(value.fetched_at); tag(['depth-chart'], false);
      show('depth-status', 'depth', `${symbol(selected)} | YES bids / implied YES asks | cumulative contracts${shownMetric ? ` | ${metric ? '' : `last known / stale metrics ${new Date(displayedMetrics.at).toLocaleTimeString()} | `}imbalance ${(shownMetric.imbalance * 100).toFixed(1)}% | microprice ${(Number(shownMetric.microprice) * 100).toFixed(2)}¢` : ''}`);
    } else retained('depth-status', 'depth', 'Full book depth unavailable.', ['depth-chart']);
    const history = value.quote_history || [];
    if (history.length) {
      draw(doc, $('probability-chart'), [{points: history.map(row => [Date.parse(row.time), Number(row.yes_bid)]), className: 'chart-green'}, {points: history.map(row => [Date.parse(row.time), Number(row.yes_ask)]), className: 'chart-red'}, {points: history.map(row => [Date.parse(row.time), Number(row.midpoint)]), className: 'chart-blue'}], {probability: true, timeAxis: true, maxGap: 6000, product: selected});
      shownAt.history = Date.parse(history.at(-1).time); tag(['probability-chart'], false);
      show('history-status', 'history', `${history.length} observations | YES bid (green), ask (red), midpoint (blue) | gaps remain visible`);
    } else retained('history-status', 'history', 'Collecting this contract\'s quote history.', ['probability-chart']);
    renderScenario(); renderSpot();
  }
  function setAsset(asset) {
    const product = ASSETS.has(asset) ? asset : `${asset}-USD`;
    if (!ASSETS.has(product)) fail('Choose BTC, ETH, SOL or XRP.');
    if (stopped || product === selected) return;
    selected = product; $('asset-select').value = product;
    generation++; controller?.abort(); fetching = false; marketState = null; lastContract = null;
    clearSpot(`Loading ${symbol(selected)} / USD.`); clearMarketCharts(); updateLabels();
    return refresh();
  }
  $('asset-select').addEventListener('change', () => {
    const asset = $('asset-select').value;
    if (!ASSETS.has(asset) || asset === selected) return;
    setAsset(asset); onAssetChange(symbol(asset));
  });
  if (ensembleEnabled) {
    $('ensemble-apply').addEventListener('click', () => {
      if (stopped) return;
      let next;
      try { next = parseEnsembleWeights(Object.fromEntries(MODEL_IDS.map(id => [id, $(`weight-${id}`).value]))); }
      catch (error) { set('ensemble-status', error.message); return; }
      weights = next; generation++; controller?.abort(); fetching = false;
      set('ensemble-status', 'Weights applied to research scenarios. The server normalizes them; Kelly inputs and orders are unchanged.');
      latest = null; clearScenario('Refreshing the ensemble with the applied weights.');
      renderSpot();
      retained('spot-status', 'spot', 'Refreshing the ensemble with the applied weights.', ['spot-chart', 'spot-value']);
      refresh();
    });
    for (const id of ['ensemble-show-paths', 'ensemble-show-components']) $(id).addEventListener('change', renderScenario);
  }
  const timer = win.setInterval(() => { if (latest && now() - Date.parse(latest.fetched_at) > 15000) retainSpot('Spot data is stale.'); setMarketState(marketState); refresh(); }, pollMs);
  function dispose() { stopped = true; generation++; controller?.abort(); win.clearInterval(timer); }
  win.addEventListener('pagehide', dispose);
  updateLabels();
  const ready = refresh();
  return {ready, refresh, dispose, setMarketState, setAsset, getAsset: () => symbol(selected)};
}


// Strategy diagnostics share this already-served module with market charts.
// A namespace keeps geometry helpers independent of the research chart model.
const strategyChartGeometry = (() => {
// Read-only chart geometry. Inputs are observed samples, never simulated fills.
const WIDTH = 760, HEIGHT = 280;
const FRAME = Object.freeze({left: 78, right: 22, top: 34, bottom: 58});
const MAX_POINTS = 720, MAX_TIME = 253402300799;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const escape = value => String(value).replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const label = (value, maximum = 180) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value)
  ? value : fail('Invalid strategy chart label.');
const number = (value, minimum, maximum) => finite(value) && value >= minimum && value <= maximum
  ? value : fail('Invalid strategy chart number.');
const nullable = (value, minimum, maximum) => value === null || value === undefined ? null : number(value, minimum, maximum);
const fixed = value => number(value, -1e15, 1e15).toFixed(2);
const tag = (name, attrs = {}, content = '') => `<${name}${Object.entries(attrs).map(([key, value]) => ` ${key}="${escape(value)}"`).join('')}>${content}</${name}>`;
const text = (x, y, content, attrs = {}) => tag('text', {x: fixed(x), y: fixed(y), class: 'chart-label', ...attrs}, escape(content));
const line = (x1, y1, x2, y2, className) => tag('line', {x1: fixed(x1), y1: fixed(y1), x2: fixed(x2), y2: fixed(y2), class: className});
const svg = (title, body) => tag('svg', {xmlns: 'http://www.w3.org/2000/svg', viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: 'img', 'aria-label': label(title)},
  tag('title', {}, escape(title)) + body);
const empty = (title, reason = 'Waiting for observed samples.') => svg(title,
  text(WIDTH / 2, HEIGHT / 2 - 3, reason, {'text-anchor': 'middle', class: 'strategy-chart-empty'})
  + text(WIDTH / 2, HEIGHT / 2 + 22, 'History is collected from validated reports; missing values remain gaps.', {'text-anchor': 'middle', class: 'strategy-chart-note'}));

function options(raw, title) {
  if (!object(raw)) fail('Invalid strategy chart options.');
  const maxGapSeconds = raw.maxGapSeconds === undefined ? 15 : number(raw.maxGapSeconds, .001, 86400);
  const timeZone = raw.timeZone === undefined ? 'UTC' : label(raw.timeZone, 80);
  let timeFormat;
  try { timeFormat = new Intl.DateTimeFormat('en-US', {timeZone, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}); }
  catch { fail('Invalid strategy chart time zone.'); }
  return {...raw, title: raw.title === undefined ? title : label(raw.title), maxGapSeconds, timeZone, timeFormat};
}

function samples(raw, fields) {
  if (!Array.isArray(raw) || raw.length > MAX_POINTS) fail('Invalid strategy chart history length.');
  let previous = -Infinity;
  return raw.map(row => {
    if (!object(row)) fail('Invalid strategy chart observation.');
    const time = number(row.time, 0, MAX_TIME);
    if (time <= previous) fail('Strategy chart observations must be strictly time ordered.');
    previous = time;
    const context = row.context === undefined || row.context === null ? null : label(row.context, 4096);
    if (row.gap !== undefined && typeof row.gap !== 'boolean') fail('Invalid strategy chart gap.');
    const point = {time, context, gap: row.gap === true};
    for (const [key, minimum, maximum] of fields) point[key] = nullable(row[key], minimum, maximum);
    return point;
  });
}

function segments(rows, fields, maxGapSeconds) {
  const result = [];
  let current = null, previous = null;
  for (const row of rows) {
    if (fields.some(key => row[key] === null)) { current = previous = null; continue; }
    if (!current || row.gap || row.context !== previous.context || row.time - previous.time > maxGapSeconds) {
      current = []; result.push(current);
    }
    current.push(row); previous = row;
  }
  return result;
}

function frame(rows, ymin, ymax, yLabel, yFormat, opts) {
  let xmin = rows[0].time, xmax = rows.at(-1).time;
  if (xmin === xmax) { xmin = Math.max(0, xmin - 2.5); xmax += 2.5; }
  const x = value => FRAME.left + (value - xmin) / (xmax - xmin) * (WIDTH - FRAME.left - FRAME.right);
  const y = value => HEIGHT - FRAME.bottom - (value - ymin) / (ymax - ymin) * (HEIGHT - FRAME.top - FRAME.bottom);
  let body = text(FRAME.left, 17, yLabel, {class: 'chart-axis-title'});
  for (const fraction of [0, .25, .5, .75, 1]) {
    const value = ymin + fraction * (ymax - ymin), at = y(value);
    body += line(FRAME.left, at, WIDTH - FRAME.right, at, 'chart-grid');
    body += text(FRAME.left - 10, at + 4, yFormat(value), {'text-anchor': 'end'});
  }
  for (const fraction of [0, .5, 1]) {
    const value = xmin + (xmax - xmin) * fraction;
    body += text(x(value), HEIGHT - 36, opts.timeFormat.format(new Date(value * 1000)), {'text-anchor': fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'});
  }
  body += text((FRAME.left + WIDTH - FRAME.right) / 2, HEIGHT - 7, `Time (${opts.timeZone})`, {'text-anchor': 'middle', class: 'chart-axis-title'});
  if (rows.length === 1) body += text(WIDTH - FRAME.right, 17, '1 observation; collecting history', {'text-anchor': 'end', class: 'strategy-chart-note'});
  return {body, x, y, xmin, xmax};
}

function series(rows, field, className, chart, opts) {
  return segments(rows, [field], opts.maxGapSeconds).map(part => {
    const path = part.length < 2 ? '' : tag('path', {d: part.map((row, index) => `${index ? 'L' : 'M'}${fixed(chart.x(row.time))},${fixed(chart.y(row[field]))}`).join(' '), class: `chart-line ${className}`});
    const last = part.at(-1);
    return path + tag('circle', {cx: fixed(chart.x(last.time)), cy: fixed(chart.y(last[field])), r: part.length === 1 ? 3.5 : 2.5, class: `chart-point ${className}`});
  }).join('');
}

function renderProbabilityChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Observed model and executable YES probabilities');
  const rows = samples(rawPoints, [['model', 0, 1], ['lower', 0, 1], ['upper', 0, 1], ['bid', 0, 1], ['ask', 0, 1]]);
  for (const row of rows) {
    if (row.lower !== null && row.upper !== null && row.lower > row.upper
        || row.lower !== null && row.model !== null && row.lower > row.model
        || row.upper !== null && row.model !== null && row.model > row.upper
        || row.bid !== null && row.ask !== null && row.bid > row.ask) fail('Inconsistent strategy chart probability bounds.');
  }
  if (!rows.length || !rows.some(row => ['model', 'lower', 'upper', 'bid', 'ask'].some(key => row[key] !== null))) return empty(opts.title);
  const chart = frame(rows, 0, 1, 'Probability / YES price (%)', value => `${(value * 100).toFixed(0)}%`, opts);
  let body = chart.body;
  for (const part of segments(rows, ['lower', 'upper'], opts.maxGapSeconds)) {
    if (part.length === 1) body += line(chart.x(part[0].time), chart.y(part[0].lower), chart.x(part[0].time), chart.y(part[0].upper), 'chart-band');
    else body += tag('polygon', {points: [...part.map(row => [row.time, row.lower]), ...[...part].reverse().map(row => [row.time, row.upper])]
      .map(([time, value]) => `${fixed(chart.x(time))},${fixed(chart.y(value))}`).join(' '), class: 'strategy-range'});
  }
  body += series(rows, 'lower', 'chart-band', chart, opts) + series(rows, 'upper', 'chart-band', chart, opts);
  body += series(rows, 'bid', 'chart-green', chart, opts) + series(rows, 'ask', 'chart-red', chart, opts);
  body += series(rows, 'model', 'chart-blue', chart, opts);
  return svg(opts.title, body);
}

function renderRollingChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Rolling residual z score and entry and exit bands');
  const entryZ = opts.entryZ === undefined ? 2 : nullable(opts.entryZ, .000001, 10);
  const exitZ = opts.exitZ === undefined ? .5 : nullable(opts.exitZ, 0, 10);
  if (entryZ === null || exitZ === null) return empty(opts.title, 'Waiting for confirmed rolling entry and exit thresholds.');
  if (exitZ >= entryZ) fail('Rolling entry threshold must exceed its exit threshold.');
  const rows = samples(rawPoints, [['z', -2e13, 2e13]]);
  const usable = rows.filter(row => row.z !== null);
  if (!usable.length) return empty(opts.title, 'Waiting for a nonzero-variance rolling reference.');
  const extent = Math.max(3, entryZ * 1.3, ...usable.map(row => Math.abs(row.z) * 1.08));
  const chart = frame(rows, -extent, extent, 'Standardized residual z (sigma)', value => Math.abs(value) >= 10000
    ? value.toExponential(1) : Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1), opts);
  let body = chart.body;
  for (const value of [-entryZ, entryZ]) body += line(FRAME.left, chart.y(value), WIDTH - FRAME.right, chart.y(value), 'strategy-threshold-entry');
  for (const value of [-exitZ, exitZ]) body += line(FRAME.left, chart.y(value), WIDTH - FRAME.right, chart.y(value), 'strategy-threshold-exit');
  body += line(FRAME.left, chart.y(0), WIDTH - FRAME.right, chart.y(0), 'strategy-zero');
  body += series(rows, 'z', 'chart-blue', chart, opts);
  body += text(FRAME.left + 9, FRAME.top + 15, `Entry +/-${entryZ}; exit +/-${exitZ}`, {class: 'strategy-chart-note'});
  return svg(opts.title, body);
}

function renderEdgeChart(rawSides, rawOptions = {}) {
  const opts = options(rawOptions, 'YES and NO conservative edge after fee allowances');
  if (!Array.isArray(rawSides) || rawSides.length > 2) fail('Invalid strategy edge comparison.');
  const seen = new Set();
  const sides = rawSides.map(raw => {
    if (!object(raw) || !['yes', 'no'].includes(raw.outcome) || seen.has(raw.outcome)) fail('Invalid strategy chart outcome.');
    seen.add(raw.outcome);
    const probability = nullable(raw.probability, 0, 1), ask = nullable(raw.ask, 0, 1), entryFee = nullable(raw.entryFee, 0, 1);
    return {outcome: raw.outcome, probability, ask, entryFee,
      edge: probability === null || ask === null || entryFee === null ? null : probability - ask - entryFee};
  });
  const entryMargin = nullable(opts.entryMargin, 0, 1), exitFee = nullable(opts.exitFee, 0, 1), exitEdge = nullable(opts.exitEdge, 0, 1);
  if (!sides.some(row => row.edge !== null)) return empty(opts.title, 'Waiting for executable prices and entry fee allowances.');
  const openingThreshold = entryMargin === null || exitFee === null || exitEdge === null ? null : entryMargin + exitFee + exitEdge;
  const roundTripThreshold = entryMargin === null || exitEdge === null ? null : entryMargin + exitEdge;
  const values = [0, ...sides.filter(row => row.edge !== null).flatMap(row => [row.edge, ...(exitFee === null ? [] : [row.edge - exitFee])]),
    ...(openingThreshold === null ? [] : [openingThreshold]), ...(roundTripThreshold === null ? [] : [roundTripThreshold])];
  let minimum = Math.min(...values), maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * .12, .01);
  minimum -= padding; maximum += padding;
  const left = 92, right = 116, top = 45, bottom = 222;
  const x = value => left + (value - minimum) / (maximum - minimum) * (WIDTH - left - right);
  let body = text(left, 17, 'Conservative edge (percentage points)', {class: 'chart-axis-title'});
  for (const fraction of [0, .25, .5, .75, 1]) {
    const value = minimum + fraction * (maximum - minimum), at = x(value);
    body += line(at, top, at, bottom, 'chart-grid');
    body += text(at, 245, `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}`, {'text-anchor': 'middle'});
  }
  body += line(x(0), top, x(0), bottom, 'strategy-zero');
  for (const [index, outcome] of ['yes', 'no'].entries()) {
    const row = sides.find(item => item.outcome === outcome), firstY = 72 + index * 83;
    body += text(16, firstY + 15, outcome.toUpperCase(), {class: 'chart-label'});
    if (!row || row.edge === null) { body += text(left + 12, firstY + 15, 'Incomplete execution data', {class: 'strategy-chart-note'}); continue; }
    for (const [offset, edge, threshold, className, caption] of [
      [0, row.edge, openingThreshold, 'strategy-edge-entry', 'Entry fee'],
      [29, exitFee === null ? null : row.edge - exitFee, roundTripThreshold, 'strategy-edge-roundtrip', 'Both fees']]) {
      const y = firstY + offset;
      if (edge === null) { body += text(left + 12, y + 14, 'Exit allowance unavailable', {class: 'strategy-chart-note'}); continue; }
      const zero = x(0), endpoint = x(edge);
      body += tag('rect', {x: fixed(Math.min(zero, endpoint)), y: fixed(y), width: fixed(Math.max(1, Math.abs(endpoint - zero))), height: 19, rx: 2, class: className},
        tag('title', {}, escape(`${outcome.toUpperCase()}: ${caption.toLowerCase()} net edge ${(edge * 100).toFixed(4)} percentage points`)));
      body += text(WIDTH - right + 10, y + 14, `${edge >= 0 ? '+' : ''}${(edge * 100).toFixed(2)} pp`, {class: 'chart-label'});
      if (threshold !== null) body += line(x(threshold), y - 3, x(threshold), y + 22, 'strategy-threshold-entry');
    }
  }
  body += text(left, 270, openingThreshold === null ? 'Entry threshold unavailable: complete policy is required.'
    : `Required: ${(openingThreshold * 100).toFixed(2)} pp after entry fee; ${(roundTripThreshold * 100).toFixed(2)} pp after both fees.`, {class: 'strategy-chart-note'});
  return svg(opts.title, body);
}

function renderPortfolioChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Observed shared paper cash, marked equity and cost-basis equity');
  const rows = samples(rawPoints, [['cash', 0, 1e12], ['equity', 0, 1e12], ['costBasis', 0, 1e12]]);
  const rawTrades = opts.trades === undefined ? [] : opts.trades;
  if (!Array.isArray(rawTrades) || rawTrades.length > 1000) fail('Invalid strategy chart execution history.');
  const trades = rawTrades.map(row => {
    if (!object(row) || !['buy', 'sell', 'flip', 'settle'].includes(row.action)) fail('Invalid strategy chart execution.');
    return {time: number(row.time, 0, MAX_TIME), action: row.action,
      outcome: row.outcome === undefined || row.outcome === null ? null : ['yes', 'no'].includes(row.outcome) ? row.outcome : fail('Invalid execution outcome.'),
      count: row.count === undefined || row.count === null ? null : number(row.count, 0, 1e12)};
  });
  const values = rows.flatMap(row => ['cash', 'equity', 'costBasis'].flatMap(key => row[key] === null ? [] : [row[key]]));
  if (!values.length) return empty(opts.title, 'Waiting for observed paper portfolio snapshots.');
  const maximum = Math.max(...values, 1);
  const chart = frame(rows, 0, maximum * 1.12, 'Paper ledger value (USD)', value => `$${value.toLocaleString('en-US', {maximumFractionDigits: maximum < 1000 ? 2 : 0})}`, opts);
  let body = chart.body;
  body += series(rows, 'costBasis', 'strategy-line-basis', chart, opts);
  body += series(rows, 'cash', 'strategy-line-cash', chart, opts);
  body += series(rows, 'equity', 'strategy-line-equity', chart, opts);
  for (const trade of trades.filter(row => row.time >= rows[0].time && row.time <= rows.at(-1).time)) {
    const at = chart.x(trade.time), y = HEIGHT - FRAME.bottom - 3;
    const className = trade.action === 'buy' ? 'strategy-trade-buy' : trade.action === 'sell' ? 'strategy-trade-sell' : 'strategy-trade-settle';
    const title = `${trade.action.toUpperCase()}${trade.outcome ? ` ${trade.outcome.toUpperCase()}` : ''}${trade.count === null ? '' : ` ${trade.count} contracts`}; execution time only`;
    body += tag('path', {d: `M${fixed(at)},${fixed(y - 8)} L${fixed(at - 4)},${fixed(y)} L${fixed(at + 4)},${fixed(y)} Z`, class: className}, tag('title', {}, escape(title)));
  }
  return svg(opts.title, body);
}

  return Object.freeze({renderProbabilityChart, renderRollingChart, renderEdgeChart, renderPortfolioChart});
})();
export const renderStrategyProbabilityChart = strategyChartGeometry.renderProbabilityChart;
export const renderStrategyRollingChart = strategyChartGeometry.renderRollingChart;
export const renderStrategyEdgeChart = strategyChartGeometry.renderEdgeChart;
export const renderStrategyPortfolioChart = strategyChartGeometry.renderPortfolioChart;
