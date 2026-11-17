// Read-only charts. No probability, risk, session token or order mutation flows
// from this module into the Kelly form.
const PRODUCTS = Object.freeze({'BTC-USD': 'KXBTC15M', 'ETH-USD': 'KXETH15M', 'SOL-USD': 'KXSOL15M', 'XRP-USD': 'KXXRP15M', GOLD: 'KXGOLD15M'});
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
  if (new Date(Date.parse(value)).toISOString().slice(0, 19) !== value.slice(0, 19)) fail('Invalid chart timestamp.');
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
  if (raw.asset === 'GOLD' && raw.status !== 'unavailable') fail('Gold underlying analytics require a connected compatible Pyth feed.');
  const result = {kind: raw.kind, version: 1, asset: raw.asset, source: label(raw.source), status: raw.status,
    fetched_at: raw.fetched_at === null ? null : iso(raw.fetched_at), error: raw.error === null ? null : label(raw.error),
    history_error: raw.history_error == null ? null : label(raw.history_error), spot: null, forecast: null};
  if (raw.status === 'ready') {
    if (!object(raw.spot) || result.fetched_at === null || !Array.isArray(raw.spot.candles) || raw.spot.candles.length > 300) fail('Incomplete spot analytics.');
    if (raw.source !== 'Coinbase Exchange spot proxy') fail('Spot analytics must identify the Coinbase proxy source.');
    const tickerReceived = Object.hasOwn(raw.spot, 'ticker_received_at') ? iso(raw.spot.ticker_received_at) : result.fetched_at;
    const historyReceived = Object.hasOwn(raw.spot, 'history_received_at')
      ? raw.spot.history_received_at === null ? null : iso(raw.spot.history_received_at)
      : raw.spot.candles.length ? result.fetched_at : null;
    if (Date.parse(tickerReceived) > Date.parse(result.fetched_at)
        || historyReceived !== null && Date.parse(historyReceived) > Date.parse(result.fetched_at)
        || raw.spot.candles.length && historyReceived === null) fail('Inconsistent spot component receipt clocks.');
    const candles = raw.spot.candles.map(row => {
      if (!object(row)) fail('Invalid candle.');
      const time = iso(row.time), opened = Date.parse(time);
      if (opened % 60000 !== 0 || opened + 60000 > Date.parse(historyReceived)) fail('Spot candles must be completed one-minute observations.');
      return {time, close: number(row.close, 'candle price')};
    });
    for (let i = 1; i < candles.length; i++) if (Date.parse(candles[i].time) <= Date.parse(candles[i - 1].time)) fail('Unordered candles.');
    result.spot = {price: number(raw.spot.price, 'spot price'), exchange_time: iso(raw.spot.exchange_time),
      ticker_received_at: tickerReceived, history_received_at: historyReceived, candles};
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
      const probabilityGrid = result.forecast.probability_fan, priceGrid = result.forecast.price_fan;
      if (probabilityGrid[0].seconds !== 0 || probabilityGrid.length !== priceGrid.length
          || probabilityGrid.some((row, index) => row.seconds !== priceGrid[index].seconds)) fail('Inconsistent research model time grids.');
      if (['p05', 'p50', 'p95'].some(key => Math.abs(probabilityGrid[0][key] - model.probability_yes) > 1e-8
          || Math.abs(priceGrid[0][key] - Number(result.spot.price)) > 1e-8 * Math.max(1, Number(result.spot.price)))) fail('Inconsistent research model starting state.');
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

// Queue balance is compared at equal distances from the verified book midpoint.
// It describes displayed liquidity, never the probability of the contract.
export function visibleDepthBalance(orderbook) {
  const depth = cumulativeDepth(orderbook);
  if (!depth.bids.length || !depth.asks.length || depth.bids[0][0] > depth.asks[0][0])
    fail('An uncrossed two-sided book is required for depth balance.');
  const midpoint = (depth.bids[0][0] + depth.asks[0][0]) / 2;
  const distances = [...new Set([...depth.bids.map(([price]) => midpoint - price),
    ...depth.asks.map(([price]) => price - midpoint)].map(value => Number(value.toFixed(12))))].sort((a, b) => a - b);
  return distances.map(distance => {
    const bids = depth.bids.filter(([price]) => midpoint - price <= distance + 1e-12).at(-1)?.[1] || 0;
    const asks = depth.asks.filter(([price]) => price - midpoint <= distance + 1e-12).at(-1)?.[1] || 0;
    return {distance, bids, asks, balance: (bids - asks) / (bids + asks)};
  }).filter(row => row.bids + row.asks > 0);
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

const chartViews = new WeakMap();
const validRange = value => object(value) && finite(value.start) && finite(value.end)
  && value.start >= 0 && value.end <= 1 && value.start < value.end;
const niceStep = raw => {
  const power = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].find(value => value * power >= raw) * power;
};

function draw(doc, svg, sourceLines, rawOptions = {}) {
  if (!svg) return;
  const {probability = false, target = null, timeAxis = false, suffix = '', maxGap = null, zeroBaseline = false, product = 'BTC-USD', xLabel = '', yLabel = '', binaryPriceAxis = false, yDomain: requestedYDomain = null, yFormat = null, xFormat = null,
    rangeContext = product, autoProbability = false, observedView = false, niceY = false} = rawOptions;
  let view = chartViews.get(svg);
  if (!view) {
    view = {hidden: new Set(), window: null}; chartViews.set(svg, view);
    svg.addEventListener('chart-range-change', event => {
      if (!validRange(event.detail) || !view.full || !view.options.timeAxis) return;
      const {start, end} = event.detail, [minimum, maximum] = view.full;
      view.window = start === 0 && end === 1 ? null : [minimum + (maximum - minimum) * start, minimum + (maximum - minimum) * end];
      draw(doc, svg, view.lines, view.options);
    });
    svg.addEventListener('chart-series-change', event => {
      const hidden = event.detail?.hiddenSeries ?? event.detail?.hidden;
      if (!Array.isArray(hidden) || hidden.some(key => typeof key !== 'string')) return;
      view.hidden = new Set(hidden); draw(doc, svg, view.lines, view.options);
    });
    svg.addEventListener('chart:view', event => {
      if (!view.options.observedView || !['all', 'observed'].includes(event.detail?.view)) return;
      view.view = event.detail.view; view.window = null; draw(doc, svg, view.lines, view.options);
    });
  }
  if (view.context !== rangeContext) { view.window = null; view.hidden.clear(); view.view = 'all'; }
  view.context = rangeContext; view.lines = sourceLines; view.options = rawOptions;
  svg.setAttribute('data-chart-scope', rangeContext);
  svg.replaceChildren();
  svg.setAttribute('viewBox', '0 0 660 240');
  if (observedView) {
    svg.setAttribute('data-chart-views', JSON.stringify([{value:'all',label:'Include scenarios'}, {value:'observed',label:'Observed only'}]));
    svg.setAttribute('data-chart-view', view.view);
  }
  const viewLines = observedView && view.view === 'observed' ? sourceLines.filter(line => line.className === 'chart-blue') : sourceLines;
  const allPoints = viewLines.flatMap(line => line.points);
  svg.setAttribute('data-chart-range-enabled', String(timeAxis && allPoints.length > 0));
  if (!allPoints.length) return;
  const fullMin = Math.min(...allPoints.map(p => p[0])), fullMax = Math.max(...allPoints.map(p => p[0]));
  view.full = [fullMin, fullMax];
  if (view.window && (view.window[1] < fullMin || view.window[0] > fullMax)) view.window = null;
  let xmin = view.window ? Math.max(fullMin, view.window[0]) : fullMin, xmax = view.window ? Math.min(fullMax, view.window[1]) : fullMax;
  const lines = viewLines.map(line => ({...line, points: line.points.filter(point => point[0] >= xmin && point[0] <= xmax)}));
  const points = lines.filter(line => !view.hidden.has(line.className)).flatMap(line => line.points);
  const fullCount = new Set(allPoints.map(point => point[0])).size, visibleCount = new Set(points.map(point => point[0])).size;
  for (const [key, value] of Object.entries({'x-min': fullMin, 'x-max': fullMax, 'window-min': xmin, 'window-max': xmax,
    'range-start': fullMax === fullMin ? 0 : (xmin-fullMin)/(fullMax-fullMin), 'range-end': fullMax === fullMin ? 1 : (xmax-fullMin)/(fullMax-fullMin),
    'visible-count': visibleCount, 'total-count': fullCount})) svg.setAttribute(`data-chart-${key}`, String(value));
  if (!points.length) {
    for (const line of lines) {
      const group = doc.createElementNS(NS, 'g'); group.setAttribute('data-chart-series', line.className || 'observed'); group.setAttribute('data-chart-label', line.label || 'Observed series');
      if (view.hidden.has(line.className)) group.setAttribute('style', 'display:none'); svg.appendChild(group);
    }
    const label = doc.createElementNS(NS, 'text'); label.setAttribute('x', '330'); label.setAttribute('y', '120'); label.setAttribute('text-anchor', 'middle'); label.setAttribute('class', 'strategy-chart-empty');
    label.textContent = lines.some(line => line.points.length) ? 'All series hidden.' : 'No observed samples in this window.'; svg.appendChild(label); return;
  }
  const yDomain = typeof requestedYDomain === 'function' ? requestedYDomain(points) : requestedYDomain;
  let ymin = yDomain ? yDomain[0] : probability && !autoProbability ? 0 : Math.min(...points.map(p => p[1])), ymax = yDomain ? yDomain[1] : probability && !autoProbability ? 1 : Math.max(...points.map(p => p[1]));
  if (target !== null) { ymin = Math.min(ymin, target); ymax = Math.max(ymax, target); }
  if (xmin === xmax) {
    if (binaryPriceAxis) { xmin = Math.max(0, xmin - .01); xmax = Math.min(1, xmax + .01); }
    else xmax = xmin + (timeAxis ? 1000 : 1);
  }
  if (zeroBaseline) ymin = 0;
  if ((!probability || autoProbability) && !yDomain) {
    const pad = Math.max((ymax - ymin) * .12, autoProbability ? .0025 : Math.abs(ymax) * .00001, .00001);
    ymin = zeroBaseline ? 0 : autoProbability ? Math.max(0, ymin-pad) : ymin-pad; ymax = autoProbability ? Math.min(1, ymax+pad) : ymax+pad;
  }
  const yStep = niceY ? niceStep((ymax-ymin)/4) : null;
  if (yStep) { ymin = Math.floor(ymin/yStep)*yStep; ymax = Math.ceil(ymax/yStep)*yStep; }
  const domain = {xmin, xmax, ymin, ymax};
  const size = {left: 78, right: 20, top: 25, bottom: binaryPriceAxis ? 58 : 42};
  const xAt = value => size.left + (value - xmin) / (xmax - xmin) * (660 - size.left - size.right);
  const yAt = value => 240 - size.bottom - (value - ymin) / (ymax - ymin) * (240 - size.top - size.bottom);
  const add = (tag, fields, content = '') => {
    const node = doc.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(fields)) node.setAttribute(key, String(value));
    node.textContent = content; svg.appendChild(node); return node;
  };
  add('g', {'data-chart-bounds': `${size.left} ${size.top} ${660-size.right} ${240-size.bottom}`});
  const bands = lines.filter(line => line.className === 'chart-band' && !view.hidden.has(line.className));
  if (bands.length === 2 && bands[0].points.length === bands[1].points.length && bands[0].points.every((point, index) => point[0] === bands[1].points[index][0])) {
    const gap = bands[0].maxGap === undefined ? maxGap : bands[0].maxGap;
    const lower = gap === null ? [bands[0].points] : chartSegments(bands[0].points, gap);
    for (const part of lower.filter(part => part.length > 1)) {
      const upper = bands[1].points.filter(point => point[0] >= part[0][0] && point[0] <= part.at(-1)[0]);
      add('polygon', {points: [...part, ...upper.toReversed()].map(([x, y]) => `${xAt(x)},${yAt(y)}`).join(' '), class: 'chart-ribbon',
        'data-chart-series': 'chart-band', 'data-chart-label': 'Scenario 5th-95th range'});
    }
  }
  const yTicks = yStep ? Array.from({length: Math.round((ymax-ymin)/yStep)+1}, (_, index) => ymin+index*yStep) : [0,.25,.5,.75,1].map(fraction => ymin+fraction*(ymax-ymin));
  for (const value of yTicks) {
    const y = yAt(value);
    add('line', {x1: size.left, x2: 640, y1: y, y2: y, class: 'chart-grid'});
    const tickStep = (ymax - ymin) / 4;
    const digits = Math.min(8, Math.max(zeroBaseline ? 0 : priceDigits(product), tickStep < 1 ? Math.ceil(-Math.log10(tickStep)) : 0));
    const compact = Math.abs(value) >= 1e6 ? value.toLocaleString('en-US', {notation: 'compact', maximumFractionDigits: 1}) : value.toLocaleString('en-US', {maximumFractionDigits: digits});
    add('text', {x: size.left - 10, y: y + 4, 'text-anchor': 'end', class: 'chart-label'}, yFormat ? yFormat(value) : probability ? `${(value * 100).toFixed(autoProbability ? 2 : 0)}%` : compact);
  }
  for (const fraction of timeAxis && xmax - xmin < 2000 ? [0, 1] : [0, .5, 1]) {
    const x = xmin + (xmax - xmin) * fraction;
    const caption = xFormat ? xFormat(x) : timeAxis ? new Date(x).toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', ...(xmax - xmin <= 120000 ? {second: '2-digit'} : {}), ...(xmax - xmin < 1000 ? {fractionalSecondDigits: 3} : {})})
      : binaryPriceAxis ? `${Number((x * 100).toFixed(Math.max(1, Math.min(6, Math.ceil(-Math.log10((xmax-xmin)*50))))))}¢` : `${x.toFixed(suffix === 's' ? 0 : 2)}${suffix}`;
    add('text', {x: xAt(x), y: binaryPriceAxis ? 208 : 219, 'text-anchor': fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle', class: 'chart-label'}, caption);
  }
  add('text', {x: size.left, y: 13, class: 'chart-axis-title'}, yLabel || (probability ? 'Probability of YES (%)' : zeroBaseline ? 'Cumulative contracts' : 'Underlying price (USD)'));
  add('text', {x: binaryPriceAxis ? (size.left + 660 - size.right) / 2 : 360, y: binaryPriceAxis ? 233 : 236, 'text-anchor': 'middle', class: 'chart-axis-title'}, xLabel || (timeAxis ? `Time (${Intl.DateTimeFormat().resolvedOptions().timeZone})` : suffix === 's' ? 'Seconds ahead' : ''));
  if (target !== null) {
    add('path', {d: linePath([[xmin, target], [xmax, target]], domain, 660, 240, size), class: 'chart-target'});
    add('text', {x: 634, y: Math.max(size.top + 13, Math.min(192, yAt(target) - 7)), 'text-anchor': 'end', class: 'chart-target-label'}, `Target ${priceLabel(target, product)}`);
  }
  for (const line of lines) {
    const seriesLabel = line.label || ({'chart-green': 'Bid', 'chart-red': 'Ask', 'chart-blue': 'Observed series', 'chart-projection': 'Scenario median', 'chart-band': 'Scenario bound', 'chart-sample': 'Simulated trajectory'}[line.className] || 'Model component');
    if (view.hidden.has(line.className)) { add('g', {'data-chart-series': line.className || 'observed', 'data-chart-label': seriesLabel, style: 'display:none'}); continue; }
    const annotate = (mark, x, y) => {
      for (const [key, value] of Object.entries({'data-chart-point': 'true', 'data-chart-series': line.className || 'observed', 'data-chart-label': seriesLabel})) mark.setAttribute(key, value);
      const title = doc.createElementNS(NS, 'title');
      const when = timeAxis ? new Date(x).toISOString() : xFormat ? xFormat(x) : `${x.toFixed(suffix === 's' ? 0 : 2)}${suffix}`;
      const value = yFormat ? yFormat(y) : probability ? `${(y * 100).toFixed(2)}%` : zeroBaseline ? `${y.toLocaleString('en-US', {maximumFractionDigits: 4})} contracts` : priceLabel(y, product);
      title.textContent = line.detail ? line.detail(x, y) : `${seriesLabel} | ${when} | ${value}`;
      mark.setAttribute('data-chart-detail', title.textContent); mark.appendChild(title);
    };
    const gap = line.maxGap === undefined ? maxGap : line.maxGap;
    const segments = gap === null ? [line.points] : chartSegments(line.points, gap);
    for (const segment of segments) {
      if (!segment.length) continue;
      const points = line.step && segment.length ? [...(line.zeroStart === false ? [] : [[segment[0][0], 0]]),
        ...segment.flatMap((point, index) => index ? [[point[0], segment[index - 1][1]], point] : [point])] : segment;
      const path = add('path', {d: linePath(points, domain, 660, 240, size), class: `chart-line ${line.className || ''}`,
        'data-chart-series': line.className || 'observed', 'data-chart-label': seriesLabel});
      const pathTitle = doc.createElementNS(NS, 'title'); pathTitle.textContent = seriesLabel; path.appendChild(pathTitle);
      if (segment.length === 1) {
        const [x, y] = segment[0];
        const point = add('circle', {cx: xAt(x), cy: yAt(y), r: 3, class: `chart-point ${line.className || ''}`});
        annotate(point, x, y);
      }
      if (segment.length > 1 && line.className !== 'chart-sample') for (const [x, y] of segment) {
        const mark = add('circle', {cx: xAt(x), cy: yAt(y), r: 6, fill: 'transparent', stroke: 'none', class: 'chart-inspect-point',
          'data-chart-point': 'true', 'data-chart-series': line.className || 'observed', 'data-chart-label': seriesLabel});
        annotate(mark, x, y);
      }
    }
  }
}

export function mountAnalytics(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 5000, onAssetChange = () => {}, asset = null} = {}) {
  if (!doc.getElementById('analytics-panel') || typeof doc.createElementNS !== 'function') return null;
  const $ = id => doc.getElementById(id), set = (id, value) => { if ($(id)) $(id).textContent = value; };
  const ensembleEnabled = $('ensemble-controls')?.dataset?.ensemble === 'true';
  let weights = ensembleEnabled ? {long_run: '1', recent: '1', ewma: '1'} : null;
  let stopped = false, generation = 0, fetching = false, controller = null, marketState = null, latest = null, lastContract = null, serviceContext = null;
  // These snapshots belong only to the view. Freshness predicates continue to
  // use the current responses, never a retained display snapshot.
  let displayedSpot = null, displayedForecast = null, displayedMarket = null, displayedMetrics = null;
  const shownAt = {spot: null, scenario: null, depth: null, history: null, spread: null, balance: null};
  const shownText = {spot: '', scenario: '', depth: '', history: '', spread: '', balance: ''};
  // Retain actual observed trades when one-minute history is unavailable.
  // Asset changes and temporary feed failures do not mix or invent prices.
  const observations = new Map();
  let selected = asset === null ? $('asset-select').value || 'BTC-USD' : ASSETS.has(asset) ? asset : `${asset}-USD`;
  if (!ASSETS.has(selected)) selected = 'BTC-USD';
  $('asset-select').value = selected;
  function updateLabels() {
    const asset = symbol(selected);
    set('spot-chart-title', selected === 'GOLD' ? 'Gold / USD per ounce' : `${asset} / USD price`);
    set('depth-chart-title', `${asset} order-book depth`);
    set('history-chart-title', `${asset} market-implied probability`);
    set('scenario-chart-title', `${asset} research probability paths`);
    set('liquidity-spread-title', `${asset} executable spread over time`);
    set('liquidity-balance-title', `${asset} depth balance around midpoint`);
    set('depth-contract-label', `${asset} contract`);
    set('ensemble-caption', `Research model comparison for the active ${asset} contract`);
    set('asset-source-description', selected === 'GOLD'
      ? 'Gold quotes and targets use the Kalshi gold contract. Settlement compares Pyth Metal.Index.1OZGOLD/USD minute closes; the underlying feed and compatible model are not connected.'
      : 'The selected asset drives the contract quotes, settlement target, order book and model charts. Coinbase spot is a proxy; crypto contracts settle against their CF Benchmarks index.');
    if ($('ensemble-controls')) $('ensemble-controls').disabled = selected === 'GOLD';
    if ($('ensemble-apply')) $('ensemble-apply').disabled = selected === 'GOLD';
    for (const [id, description] of Object.entries({'spot-chart': `${asset} underlying price in USD with its current settlement target`, 'depth-chart': `${asset} cumulative Kalshi order-book depth against YES price in cents`, 'probability-chart': `${asset} observed YES bid, ask and midpoint probabilities`, 'scenario-chart': `${asset} research terminal-spot probability paths and model quantiles`,
      'liquidity-spread-chart': `${asset} observed executable YES ask minus bid in percentage points`,
      'liquidity-balance-chart': `${asset} normalized displayed YES bid and ask queue balance at equal distance from the current midpoint`})) $(id)?.setAttribute('aria-label', description);
  }
  function clearMarketCharts(message = `Loading the selected ${symbol(selected)} contract.`) {
    shownAt.depth = null; shownAt.history = null; shownAt.spread = null; shownAt.balance = null;
    shownText.depth = ''; shownText.history = ''; shownText.spread = ''; shownText.balance = '';
    displayedMarket = null; displayedMetrics = null;
    $('depth-chart').replaceChildren(); $('probability-chart').replaceChildren();
    $('liquidity-spread-chart')?.replaceChildren(); $('liquidity-balance-chart')?.replaceChildren();
    for (const id of ['depth-chart', 'probability-chart', 'liquidity-spread-chart', 'liquidity-balance-chart']) resetView($(id));
    tag(['depth-chart', 'probability-chart', 'liquidity-spread-chart', 'liquidity-balance-chart'], false);
    set('depth-status', message); set('history-status', message);
    set('liquidity-spread-status', message); set('liquidity-balance-status', message);
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
    resetView($('spot-chart'));
    clearScenario();
  }
  function resetView(svg) {
    if (!svg) return;
    const view = chartViews.get(svg);
    if (view) { view.window = null; view.hidden.clear(); view.lines = []; view.full = null; }
    svg.setAttribute('data-chart-range-enabled', 'false');
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
  function contractScope(market = displayedMarket?.market) {
    return `${serviceContext}|${selected}|${market?.ticker || ''}|${market?.quote_id || ''}`;
  }
  function matching(model) {
    const market = currentMarket();
    const anchor = model?.status === 'ready' ? Date.parse(model.close_time) - model.probability_fan.at(-1).seconds * 1000 : NaN;
    const anchorAge = now() - anchor;
    return market && model?.status === 'ready' && Date.parse(market.close_time) - now() > 60000
      && anchorAge >= -1000 && anchorAge <= 15000
      && market.quote_id === model.quote_id && market.ticker === model.contract_ticker
      && canonical(market.target_price) === canonical(model.target_price) && Date.parse(market.close_time) === Date.parse(model.close_time);
  }
  function renderSpot() {
    if (!displayedSpot?.spot) return;
    const value = displayedSpot;
    const points = value.spot.candles.map(row => [Date.parse(row.time) + 60000, Number(row.close)]);
    const pointSources = new Map(value.spot.candles.map(row => [Date.parse(row.time) + 60000,
      `Completed one-minute candle close; candle opened ${new Date(row.time).toISOString()}`]));
    for (const tick of observations.get(value.asset) || []) {
      if (points.at(-1)?.[0] === tick[0]) { points[points.length - 1] = tick; pointSources.set(tick[0], 'Observed trade; exchange event timestamp'); }
      else if (!points.length || tick[0] > points.at(-1)[0]) { points.push(tick); pointSources.set(tick[0], 'Observed trade; exchange event timestamp'); }
    }
    const target = displayedMarket ? Number(displayedMarket.market.target_price) : null;
    const lines = [{points, className: 'chart-blue', label: 'Observed Coinbase spot proxy',
      detail: (time, price) => `${value.source} | ${pointSources.get(time)} | ${new Date(time).toISOString()} | ${priceLabel(price, selected)} | Not the contract settlement source`}];
    if (displayedForecast && displayedMarket?.market.quote_id === displayedForecast.model.quote_id) {
      const model = displayedForecast.model;
      const modelTime = Date.parse(model.close_time) - model.price_fan.at(-1).seconds * 1000;
      for (const key of ['p05', 'p50', 'p95']) lines.push({points: model.price_fan.map(row => [modelTime + row.seconds * 1000, row[key]]), maxGap: null, className: key === 'p50' ? 'chart-projection' : 'chart-band', label: key === 'p50' ? 'Scenario median' : 'Scenario 5th-95th range',
        detail: (time, price) => `Coinbase terminal-spot research ${key} | model anchor ${new Date(modelTime).toISOString()} | ${((time-modelTime)/1000).toFixed(1)} seconds ahead (${new Date(time).toISOString()}) | ${priceLabel(price, selected)} | Conditional scenario quantile, not calibrated confidence`});
    }
    draw(doc, $('spot-chart'), lines, {target, timeAxis: true, maxGap: 90000, product: selected, observedView: true,
      rangeContext: contractScope()});
    const history = value.retained_history_at
      ? `Minute history last known / stale since ${new Date(value.retained_history_at).toLocaleTimeString()}; observed live trades continue`
      : value.history_error || !value.spot.candles.length
      ? 'Minute history unavailable; collecting live trades since this page opened.'
      : '1-minute closes plus observed live trades';
    const tickerAge = now() - Date.parse(value.spot.ticker_received_at), exchangeAge = now() - Date.parse(value.spot.exchange_time);
    const staleSpot = !latest || !Number.isFinite(tickerAge) || tickerAge < -1000 || tickerAge > 15000 || !Number.isFinite(exchangeAge) || exchangeAge < -1000 || exchangeAge > 60000;
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
    if (selected === 'GOLD') {
      clearScenario('Gold minute-close settlement requires a compatible Pyth model. The crypto CF-average and Coinbase research models are not used.');
      return;
    }
    const model = latest?.forecast;
    if (!matching(model) || !latest || now() - Date.parse(latest.spot.ticker_received_at) > 15000 || now() >= Date.parse(model.close_time)) {
      retained('scenario-status', 'scenario', model?.status === 'unavailable' ? model.reason : `Waiting for a fresh scenario matched to the active ${symbol(selected)} contract.`, ['scenario-chart', 'scenario-value', 'ensemble-members']); return;
    }
    if (Date.parse(model.close_time) - now() <= 60000) { retained('scenario-status', 'scenario', 'Final settlement minute: waiting for a replacement settlement estimate.', ['scenario-chart', 'scenario-value', 'ensemble-members']); return; }
    displayedForecast = {model, fetched_at: latest.fetched_at}; shownAt.scenario = Date.parse(latest.fetched_at);
    $('scenario-chart').replaceChildren(); clearMembers();
    tag(['scenario-chart', 'scenario-value', 'ensemble-members'], false);
    set('scenario-value', `${(model.probability_yes * 100).toFixed(1)}%`);
    const isEnsemble = model.model === 'ensemble_log_diffusion';
    show('scenario-status', 'scenario', isEnsemble
      ? `${model.components.filter(component => component.weight > 0).length} active of ${model.components.length} models; ${model.simulation_count} simulated trajectories; 5th / 50th / 95th mixture percentiles. Current probability uses normalized starting weights; future paths update model weights from each simulated price path. Not a calibrated settlement forecast.`
      : `Terminal spot above target scenario. ${model.sample_count} closed returns; 5th / 50th / 95th percentiles. Not a calibrated settlement forecast.`);
    const lines = [];
    if (isEnsemble && ensembleEnabled) {
      if ($('ensemble-show-paths').checked) {
        const count = Math.min(6, model.paths.length);
        const paths = Array.from({length: count}, (_, index) => model.paths[count === 1 ? 0 : Math.round(index * (model.paths.length-1) / (count-1))]);
        for (const path of paths) lines.push({points: path.points.map(row => [row.seconds, row.probability_yes]), className: 'chart-sample', label: 'Selected trajectories across terminal-price ranks (up to 6; not frequencies)'});
      }
      if ($('ensemble-show-components').checked) for (const component of model.components) if (component.probability_path.length) lines.push({points: component.probability_path.map(row => [row.seconds, row.probability_yes]), className: `chart-component-${component.id}`, label: component.label});
      const make = tag => typeof doc.createElement === 'function' ? doc.createElement(tag) : doc.createElementNS('http://www.w3.org/1999/xhtml', tag);
      for (const component of model.components) {
        const row = make('tr');
        for (const value of [component.label, `${(component.weight * 100).toFixed(1)}%`, component.probability_yes === null ? 'Unavailable (excluded)' : `${(component.probability_yes * 100).toFixed(1)}%`, component.volatility_per_sqrt_second === 0 ? 'No variation' : `${(component.volatility_per_sqrt_second * Math.sqrt(60) * 100).toFixed(4)}%`]) {
          const cell = make('td'); cell.textContent = value; row.appendChild(cell);
        }
        $('ensemble-members').appendChild(row);
      }
    }
    const anchor = Date.parse(model.close_time) - model.probability_fan.at(-1).seconds * 1000;
    lines.push(...['p05', 'p95', 'p50'].map(key => ({points: model.probability_fan.map(row => [row.seconds, row[key]]), className: key === 'p50' ? 'chart-blue' : 'chart-band', label: key === 'p50' ? 'Mixture median (scenario)' : 'Scenario 5th-95th range',
      detail: (seconds, probability) => `Terminal-spot research probability ${key} | ${(probability*100).toFixed(4)}% | ${seconds.toFixed(1)} seconds ahead | anchor ${new Date(anchor).toISOString()} | ${model.contract_ticker} | Scenario quantile, not a calibrated settlement probability interval`})));
    draw(doc, $('scenario-chart'), lines, {probability: true, suffix: 's', product: selected,
      rangeContext: `${contractScope()}|${model.model}|${JSON.stringify(weights)}`});
  }
  async function refresh() {
    if (stopped || fetching) return;
    if (selected === 'GOLD') {
      clearSpot('Pyth Metal.Index.1OZGOLD/USD feed is not connected. Gold quotes and contract-price history remain available below.');
      clearScenario('Gold minute-close settlement requires a compatible Pyth model. The crypto CF-average and Coinbase research models are not used.');
      return;
    }
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
      const age = now() - Date.parse(value.spot.ticker_received_at), exchangeAge = now() - Date.parse(value.spot.exchange_time);
      if (age < -1000 || age > 15000 || exchangeAge < -1000 || exchangeAge > 60000) fail('Spot data is stale.');
      const newestTradeTime = observations.get(asset)?.at(-1)?.[0];
      if (Date.parse(value.spot.exchange_time) < (newestTradeTime ?? -Infinity)
          || displayedSpot && (Date.parse(value.fetched_at) < Date.parse(displayedSpot.fetched_at)
          || Date.parse(value.spot.exchange_time) < Date.parse(displayedSpot.spot.exchange_time))) fail('An older spot response cannot replace the newer observed value.');
      recordTicker(value);
      latest = value;
      const keepHistory = !value.spot.candles.length && displayedSpot?.spot.candles.length;
      displayedSpot = {...value, spot: {...value.spot, candles: keepHistory ? displayedSpot.spot.candles : value.spot.candles},
        retained_history_at: keepHistory ? displayedSpot.retained_history_at || displayedSpot.spot.history_received_at || displayedSpot.fetched_at : null};
      shownAt.spot = Date.parse(value.spot.exchange_time);
      renderScenario();
      set('spot-value', priceLabel(value.spot.price, selected));
      renderSpot();
    } catch (error) {
      if (!stopped && expected === generation) retainSpot(error.message || 'Spot feed unavailable.');
    } finally { win.clearTimeout(timeout); if (expected === generation) { fetching = false; controller = null; } }
  }
  function setMarketState(value) {
    const context = value && ['production', 'demo'].includes(value.environment) && ['paper', 'demo', 'live'].includes(value.mode)
      ? `${value.environment}|${value.mode}` : null;
    if (context !== null && serviceContext !== null && context !== serviceContext) {
      generation++; controller?.abort(); fetching = false; latest = null; lastContract = null;
      clearMarketCharts('Loading the selected service environment.');
      clearScenario('Waiting for a model bound to the selected service environment.');
    }
    if (context !== null) serviceContext = context;
    marketState = value;
    const incoming = value?.market_status === 'ready' && value.market?.ticker?.startsWith(PRODUCTS[selected] + '-')
      && (!value.selected_asset || value.selected_asset === symbol(selected)) ? value.market : null;
    if (incoming && displayedMarket && Date.parse(value.fetched_at) < Date.parse(displayedMarket.fetched_at)) {
      marketState = null;
      for (const [status, kind, ids] of [
        ['depth-status', 'depth', ['depth-chart']], ['history-status', 'history', ['probability-chart']],
        ['liquidity-balance-status', 'balance', ['liquidity-balance-chart']], ['liquidity-spread-status', 'spread', ['liquidity-spread-chart']]])
        retained(status, kind, 'An older quote response was ignored; waiting for a current observation.', ids);
      renderScenario(); renderSpot(); return;
    }
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
      retained('liquidity-balance-status', 'balance', 'Fresh two-sided book unavailable.', ['liquidity-balance-chart']);
      retained('liquidity-spread-status', 'spread', 'Waiting for current contract observations.', ['liquidity-spread-chart']);
      if ($('rollover-status')) set('rollover-status', `Watching for the next verified active ${symbol(selected)} window. Market discovery retries automatically every 2 seconds.`);
      renderScenario(); renderSpot(); return;
    }
    displayedMarket = {market: value.market, fetched_at: value.fetched_at};
    if ($('rollover-status')) set('rollover-status', changed
      ? `New ${symbol(selected)} window loaded. Contract history and the previous window's estimate were reset; spot history continues.`
      : `Watching the active ${symbol(selected)} window. The next verified contract loads automatically when it opens.`);
    if (value.market.orderbook) {
      const depth = cumulativeDepth(value.market.orderbook);
      const depthDetail = side => (price, quantity) => `${symbol(selected)} | ${value.market.ticker} | ${side} | ${(price*100).toFixed(2)} cents per contract | ${quantity.toLocaleString('en-US', {maximumFractionDigits: 4})} cumulative contracts | quote received ${new Date(value.fetched_at).toISOString()} | Displayed liquidity, not outcome probability`;
      draw(doc, $('depth-chart'), [{points: depth.bids, className: 'chart-green', step: true, label: 'Displayed YES bids', detail: depthDetail('YES bids')}, {points: depth.asks, className: 'chart-red', step: true, label: 'Displayed YES asks', detail: depthDetail('YES asks derived from NO bids')}], {zeroBaseline: true, binaryPriceAxis: true, xLabel: 'YES contract price (¢)', product: selected, rangeContext: contractScope(value.market)});
      const metric = value.market.metrics;
      if (metric) displayedMetrics = {value: metric, at: Date.parse(value.fetched_at)};
      const shownMetric = displayedMetrics?.value;
      shownAt.depth = Date.parse(value.fetched_at); tag(['depth-chart'], false);
      show('depth-status', 'depth', `${symbol(selected)} | YES bids / implied YES asks | cumulative contracts${shownMetric ? ` | ${metric ? '' : `last known / stale metrics ${new Date(displayedMetrics.at).toLocaleTimeString()} | `}imbalance ${(shownMetric.imbalance * 100).toFixed(1)}% | microprice ${(Number(shownMetric.microprice) * 100).toFixed(2)}¢` : ''}`);
      try {
        const balance = visibleDepthBalance(value.market.orderbook);
        draw(doc, $('liquidity-balance-chart'), [{points: balance.map(row => [row.distance, row.balance]),
          className: 'chart-blue', label: 'Displayed queue balance', step: true, zeroStart: false, detail: distance => {
            const row = balance.find(row => row.distance === distance);
            return `Displayed queue balance | within ${(distance * 100).toFixed(2)} pp of midpoint | bid ${row.bids.toLocaleString()} contracts; ask ${row.asks.toLocaleString()} contracts | ${(row.balance * 100).toFixed(2)}%`;
          }}], {yDomain: [-1, 1], yFormat: value => `${(value * 100).toFixed(0)}%`,
          xFormat: value => `${(value * 100).toFixed(1)} pp`, xLabel: 'Equal price distance from midpoint (pp)',
          yLabel: 'Displayed bid-ask queue balance (%)', product: selected, rangeContext: contractScope(value.market)});
        shownAt.balance = Date.parse(value.fetched_at); tag(['liquidity-balance-chart'], false);
        show('liquidity-balance-status', 'balance', `${symbol(selected)} | ${balance.length} observed depth bands | (bid size − ask size) / total visible size at equal distance from midpoint. Displayed liquidity only.`);
      } catch {
        retained('liquidity-balance-status', 'balance', 'An uncrossed two-sided book is required; missing queue sizes are not inferred.', ['liquidity-balance-chart']);
      }
    } else {
      retained('depth-status', 'depth', 'Full book depth unavailable.', ['depth-chart']);
      retained('liquidity-balance-status', 'balance', 'Full two-sided book unavailable.', ['liquidity-balance-chart']);
    }
    const history = value.quote_history || [];
    if (history.length) {
      const rangeContext = contractScope(value.market);
      draw(doc, $('probability-chart'), [{points: history.map(row => [Date.parse(row.time), Number(row.yes_bid)]), className: 'chart-green', label: 'Observed YES bid'}, {points: history.map(row => [Date.parse(row.time), Number(row.yes_ask)]), className: 'chart-red', label: 'Observed YES ask'}, {points: history.map(row => [Date.parse(row.time), Number(row.midpoint)]), className: 'chart-blue', label: 'Observed YES midpoint'}], {probability: true, autoProbability: true, timeAxis: true, maxGap: 6000, product: selected,
        yLabel: 'YES contract price (%; auto-scaled)', rangeContext});
      shownAt.history = Date.parse(history.at(-1).time); tag(['probability-chart'], false);
      const historySpan = (Date.parse(history.at(-1).time) - Date.parse(history[0].time)) / 60000;
      show('history-status', 'history', `${history.length} current-contract quote observations over ${historySpan.toFixed(2)} min | YES bid (green), ask (red), midpoint (blue) | ${new Date(history[0].time).toISOString()} to ${new Date(history.at(-1).time).toISOString()}. Collected by this console; contract changes and service restarts reset this history. Earlier quotes are not backfilled; gaps remain visible.`);
      const spreads = history.map(row => [Date.parse(row.time), (Number(row.yes_ask) - Number(row.yes_bid)) * 100]);
      draw(doc, $('liquidity-spread-chart'), [{points: spreads, className: 'chart-blue', label: 'Observed executable spread'}],
        {timeAxis: true, maxGap: 6000, zeroBaseline: true, niceY: true, yDomain: points => [0, Math.max(1, ...points.map(point => point[1] * 1.05))], yLabel: 'YES ask - bid (percentage points)',
          yFormat: value => `${value.toFixed(2)} pp`, product: selected, rangeContext});
      shownAt.spread = Date.parse(history.at(-1).time); tag(['liquidity-spread-chart'], false);
      show('liquidity-spread-status', 'spread', `${history.length} current-contract quotes | latest spread ${spreads.at(-1)[1].toFixed(2)} pp before fees | observation gaps stay visible.`);
    } else {
      retained('history-status', 'history', 'Collecting this contract\'s quote history.', ['probability-chart']);
      retained('liquidity-spread-status', 'spread', 'Collecting this contract\'s quote history.', ['liquidity-spread-chart']);
    }
    renderScenario(); renderSpot();
  }
  function setAsset(asset) {
    const product = ASSETS.has(asset) ? asset : `${asset}-USD`;
    if (!ASSETS.has(product)) fail('Choose BTC, ETH, SOL, XRP or GOLD.');
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
  const timer = win.setInterval(() => { if (latest && now() - Date.parse(latest.spot.ticker_received_at) > 15000) retainSpot('Spot data is stale.'); setMarketState(marketState); refresh(); }, pollMs);
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
const line = (x1, y1, x2, y2, className, attrs = {}) => tag('line', {x1: fixed(x1), y1: fixed(y1), x2: fixed(x2), y2: fixed(y2), class: className, ...attrs});
const svg = (title, body, opts = {}) => tag('svg', {xmlns: 'http://www.w3.org/2000/svg', viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: 'img', 'aria-label': label(title),
  'data-chart-range-enabled': Boolean(opts.viewport), ...(opts.rangeMetadata || {})},
  tag('title', {}, escape(title)) + body);
const empty = (title, reason = 'Waiting for observed samples.', opts = {}) => svg(title,
  text(WIDTH / 2, HEIGHT / 2 - 3, reason, {'text-anchor': 'middle', class: 'strategy-chart-empty'})
  + text(WIDTH / 2, HEIGHT / 2 + 22, 'History is collected from validated reports; missing values remain gaps.', {'text-anchor': 'middle', class: 'strategy-chart-note'}), opts);

function options(raw, title) {
  if (!object(raw)) fail('Invalid strategy chart options.');
  const maxGapSeconds = raw.maxGapSeconds === undefined ? 15 : number(raw.maxGapSeconds, .001, 86400);
  const timeZone = raw.timeZone === undefined ? 'UTC' : label(raw.timeZone, 80);
  let timeFormat;
  try { timeFormat = new Intl.DateTimeFormat('en-US', {timeZone, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}); }
  catch { fail('Invalid strategy chart time zone.'); }
  if (raw.range !== undefined && (!object(raw.range) || !finite(raw.range.start) || !finite(raw.range.end)
      || raw.range.start < 0 || raw.range.end > 1 || raw.range.start >= raw.range.end)) fail('Invalid strategy chart range.');
  if (raw.window !== undefined && (!object(raw.window) || !finite(raw.window.minimum) || !finite(raw.window.maximum)
      || raw.window.minimum < 0 || raw.window.maximum > MAX_TIME || raw.window.minimum >= raw.window.maximum)) fail('Invalid strategy chart window.');
  return {...raw, title: raw.title === undefined ? title : label(raw.title), maxGapSeconds, timeZone, timeFormat};
}

function windowRows(rows, opts) {
  if (!rows.length) return rows;
  const minimum = rows[0].time, maximum = rows.at(-1).time, span = maximum-minimum;
  let low = opts.window?.minimum ?? minimum + span*(opts.range?.start ?? 0);
  let high = opts.window?.maximum ?? minimum + span*(opts.range?.end ?? 1);
  if (high < minimum || low > maximum) { low = minimum; high = maximum; }
  low = Math.max(minimum, low); high = Math.min(maximum, high);
  const selected = rows.filter(row => row.time >= low && row.time <= high);
  opts.viewport = {minimum: low, maximum: high};
  opts.rangeMetadata = {'data-chart-x-min': minimum, 'data-chart-x-max': maximum,
    'data-chart-window-min': low, 'data-chart-window-max': high,
    'data-chart-range-start': span ? (low-minimum)/span : 0, 'data-chart-range-end': span ? (high-minimum)/span : 1,
    'data-chart-visible-count': selected.length, 'data-chart-total-count': rows.length};
  return selected;
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
    for (const key of ['modelTime', 'quoteTime']) if (row[key] !== undefined && row[key] !== null) {
      point[key] = number(row[key], 0, MAX_TIME);
      if (point[key] > time) fail('Strategy chart source clock is later than its observation.');
    }
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
  let xmin = opts.viewport?.minimum ?? rows[0].time, xmax = opts.viewport?.maximum ?? rows.at(-1).time;
  if (xmin === xmax) { xmin = Math.max(0, xmin - 2.5); xmax += 2.5; }
  const x = value => FRAME.left + (value - xmin) / (xmax - xmin) * (WIDTH - FRAME.left - FRAME.right);
  const y = value => HEIGHT - FRAME.bottom - (value - ymin) / (ymax - ymin) * (HEIGHT - FRAME.top - FRAME.bottom);
  let body = tag('g', {'data-chart-bounds': `${FRAME.left} ${FRAME.top} ${WIDTH-FRAME.right} ${HEIGHT-FRAME.bottom}`}) + text(FRAME.left, 17, yLabel, {class: 'chart-axis-title'});
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
  if (rows.length === 1) body += text(WIDTH - FRAME.right, 17, opts.rangeMetadata?.['data-chart-total-count'] > 1 ? '1 observation in selected window' : '1 observation; collecting history', {'text-anchor': 'end', class: 'strategy-chart-note'});
  return {body, x, y, xmin, xmax};
}

function series(rows, field, className, chart, opts) {
  const labels = {model: 'Settlement ensemble P(YES)', lower: 'Lower sensitivity scenario', upper: 'Upper sensitivity scenario',
    bid: 'Executable YES bid', ask: 'Executable YES ask', z: 'Rolling residual z score',
    cash: 'Paper cash', equity: 'Net marked paper equity', costBasis: 'Cash plus recorded open basis'};
  const key = field === 'lower' || field === 'upper' ? 'sensitivity' : field;
  const seriesLabel = key === 'sensitivity' ? 'Model sensitivity range' : labels[field] || field;
  const detail = row => `${labels[field] || field} | ${new Date(row.time * 1000).toISOString()} | ${['model', 'lower', 'upper', 'bid', 'ask'].includes(field)
    ? `${(row[field] * 100).toFixed(2)}%` : field === 'z' ? `${row[field].toLocaleString('en-US', {maximumFractionDigits: 4})} sigma`
      : `$${row[field].toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 4})}`}${row.modelTime === undefined ? '' : ` | model as of ${new Date(row.modelTime*1000).toISOString()}`}${row.quoteTime === undefined ? '' : ` | quote received ${new Date(row.quoteTime*1000).toISOString()}`}`;
  return segments(rows, [field], opts.maxGapSeconds).map(part => {
    const path = part.length < 2 ? '' : tag('path', {d: part.map((row, index) => `${index ? 'L' : 'M'}${fixed(chart.x(row.time))},${fixed(chart.y(row[field]))}`).join(' '), class: `chart-line ${className}`,
      'data-chart-series': key, 'data-chart-label': seriesLabel}, tag('title', {}, escape(seriesLabel)));
    const last = part.at(-1);
    const markers = part.map(row => tag('circle', {cx: fixed(chart.x(row.time)), cy: fixed(chart.y(row[field])), r: row === last ? part.length === 1 ? 3.5 : 2.5 : 6,
      ...(row === last ? {} : {fill: 'transparent', stroke: 'none'}), class: row === last ? `chart-point ${className}` : 'chart-inspect-point',
      'data-chart-point': 'true', 'data-chart-series': key, 'data-chart-label': seriesLabel, 'data-chart-detail': detail(row)}, tag('title', {}, escape(detail(row))))).join('');
    return path + markers;
  }).join('');
}

function renderProbabilityChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Observed model and executable YES probabilities');
  const allRows = samples(rawPoints, [['model', 0, 1], ['lower', 0, 1], ['upper', 0, 1], ['bid', 0, 1], ['ask', 0, 1]]);
  for (const row of allRows) {
    if (row.lower !== null && row.upper !== null && row.lower > row.upper
        || row.lower !== null && row.model !== null && row.lower > row.model
        || row.upper !== null && row.model !== null && row.model > row.upper
        || row.bid !== null && row.ask !== null && row.bid > row.ask) fail('Inconsistent strategy chart probability bounds.');
  }
  const rows = windowRows(allRows, opts);
  if (!rows.length || !rows.some(row => ['model', 'lower', 'upper', 'bid', 'ask'].some(key => row[key] !== null))) return empty(opts.title,
    allRows.length && !rows.length ? 'No observed samples in this window.' : 'Waiting for observed samples.', opts);
  const chart = frame(rows, 0, 1, 'Probability / YES price (%)', value => `${(value * 100).toFixed(0)}%`, opts);
  let body = chart.body;
  for (const part of segments(rows, ['lower', 'upper'], opts.maxGapSeconds)) {
    if (part.length === 1) body += line(chart.x(part[0].time), chart.y(part[0].lower), chart.x(part[0].time), chart.y(part[0].upper), 'chart-band',
      {'data-chart-series': 'sensitivity', 'data-chart-label': 'Model sensitivity range'});
    else body += tag('polygon', {points: [...part.map(row => [row.time, row.lower]), ...[...part].reverse().map(row => [row.time, row.upper])]
      .map(([time, value]) => `${fixed(chart.x(time))},${fixed(chart.y(value))}`).join(' '), class: 'strategy-range', 'data-chart-series': 'sensitivity', 'data-chart-label': 'Model sensitivity range'});
  }
  body += series(rows, 'lower', 'chart-band', chart, opts) + series(rows, 'upper', 'chart-band', chart, opts);
  body += series(rows, 'bid', 'chart-green', chart, opts) + series(rows, 'ask', 'chart-red', chart, opts);
  body += series(rows, 'model', 'chart-blue', chart, opts);
  return svg(opts.title, body, opts);
}

function renderRollingChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Rolling residual z score and entry and exit bands');
  const entryZ = opts.entryZ === undefined ? 2 : nullable(opts.entryZ, Number.MIN_VALUE, 10);
  const exitZ = opts.exitZ === undefined ? .5 : nullable(opts.exitZ, 0, 10);
  if (entryZ === null || exitZ === null) return empty(opts.title, 'Waiting for confirmed rolling entry and exit thresholds.');
  if (exitZ >= entryZ) fail('Rolling entry threshold must exceed its exit threshold.');
  const allRows = samples(rawPoints, [['z', -1e100, 1e100]]), rows = windowRows(allRows, opts);
  const usable = rows.filter(row => row.z !== null);
  if (!usable.length) return empty(opts.title, allRows.length && !rows.length ? 'No observed samples in this window.' : 'Waiting for a nonzero-variance rolling reference.', opts);
  const extent = Math.max(3, entryZ * 1.3, ...usable.map(row => Math.abs(row.z) * 1.08));
  const chart = frame(rows, -extent, extent, 'Standardized residual z (sigma)', value => Math.abs(value) >= 10000
    ? value.toExponential(1) : Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1), opts);
  let body = chart.body;
  for (const value of [-entryZ, entryZ]) body += line(FRAME.left, chart.y(value), WIDTH - FRAME.right, chart.y(value), 'strategy-threshold-entry',
    {'data-chart-series': 'entry-threshold', 'data-chart-label': 'Entry threshold'});
  for (const value of [-exitZ, exitZ]) body += line(FRAME.left, chart.y(value), WIDTH - FRAME.right, chart.y(value), 'strategy-threshold-exit',
    {'data-chart-series': 'exit-threshold', 'data-chart-label': 'Exit threshold'});
  body += line(FRAME.left, chart.y(0), WIDTH - FRAME.right, chart.y(0), 'strategy-zero');
  body += series(rows, 'z', 'chart-blue', chart, opts);
  body += text(FRAME.left + 9, FRAME.top + 15, `Entry +/-${entryZ}; exit +/-${exitZ}`, {class: 'strategy-chart-note'});
  return svg(opts.title, body, opts);
}

function renderEdgeChart(rawSides, rawOptions = {}) {
  const opts = options(rawOptions, 'YES and NO conservative edge after fee allowances');
  if (!Array.isArray(rawSides) || rawSides.length > 2) fail('Invalid strategy edge comparison.');
  const seen = new Set();
  const sides = rawSides.map(raw => {
    if (!object(raw) || !['yes', 'no'].includes(raw.outcome) || seen.has(raw.outcome)) fail('Invalid strategy chart outcome.');
    seen.add(raw.outcome);
    const probability = nullable(raw.probability, 0, 1), rawProbability = nullable(raw.rawProbability, 0, 1),
      ask = nullable(raw.ask, 0, 1), entryFee = nullable(raw.entryFee, 0, 1);
    return {outcome: raw.outcome, probability, rawProbability, ask, entryFee,
      rawEdge: rawProbability === null || ask === null ? null : rawProbability - ask,
      edge: probability === null || ask === null || entryFee === null ? null : probability - ask - entryFee};
  });
  const entryMargin = nullable(opts.entryMargin, 0, 1), exitFee = nullable(opts.exitFee, 0, 1), exitEdge = nullable(opts.exitEdge, 0, 1);
  if (!sides.some(row => row.edge !== null || row.rawEdge !== null)) return empty(opts.title, 'Waiting for executable prices and entry fee allowances.');
  const openingThreshold = entryMargin === null || exitFee === null || exitEdge === null ? null : entryMargin + exitFee + exitEdge;
  const roundTripThreshold = entryMargin === null || exitEdge === null ? null : entryMargin + exitEdge;
  const values = [0, ...sides.flatMap(row => [...(row.rawEdge === null ? [] : [row.rawEdge]),
    ...(row.edge === null ? [] : [row.edge, ...(exitFee === null ? [] : [row.edge - exitFee])])]),
    ...(openingThreshold === null ? [] : [openingThreshold]), ...(roundTripThreshold === null ? [] : [roundTripThreshold])];
  let minimum = Math.min(...values), maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * .12, .01);
  minimum -= padding; maximum += padding;
  const tickStep = niceStep((maximum-minimum)/4);
  minimum = Math.floor(minimum/tickStep)*tickStep; maximum = Math.ceil(maximum/tickStep)*tickStep;
  const left = 92, right = 116, top = 45, bottom = 222;
  const x = value => left + (value - minimum) / (maximum - minimum) * (WIDTH - left - right);
  let body = text(left, 17, 'Directional edge (percentage points)', {class: 'chart-axis-title'});
  for (let index=0; index<=Math.round((maximum-minimum)/tickStep); index++) {
    const value = minimum + index*tickStep, at = x(value);
    body += line(at, top, at, bottom, 'chart-grid');
    body += text(at, 245, `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}`, {'text-anchor': 'middle'});
  }
  body += line(x(0), top, x(0), bottom, 'strategy-zero');
  for (const [index, outcome] of ['yes', 'no'].entries()) {
    const row = sides.find(item => item.outcome === outcome), firstY = 62 + index * 83;
    body += text(16, firstY + 12, outcome === 'yes' ? 'BUY YES' : 'BUY NO', {class: 'chart-label'});
    body += text(16, firstY + 28, outcome === 'yes' ? 'long YES' : 'short YES', {class: 'strategy-chart-note'});
    if (!row || row.edge === null && row.rawEdge === null) { body += text(left + 12, firstY + 15, 'Incomplete execution data', {class: 'strategy-chart-note'}); continue; }
    for (const [offset, edge, threshold, className, caption, missing] of [
      [0, row.rawEdge, null, 'strategy-edge-raw', 'raw model pre-fee', 'Raw model edge unavailable'],
      [23, row.edge, openingThreshold, 'strategy-edge-entry', 'conservative after entry fee', 'Entry allowance unavailable'],
      [46, exitFee === null || row.edge === null ? null : row.edge - exitFee, roundTripThreshold, 'strategy-edge-roundtrip', 'conservative after both fees', 'Exit allowance unavailable']]) {
      const y = firstY + offset;
      if (edge === null) { body += text(left + 12, y + 11, missing, {class: 'strategy-chart-note'}); continue; }
      const zero = x(0), endpoint = x(edge);
      body += tag('rect', {x: fixed(Math.min(zero, endpoint)), y: fixed(y), width: fixed(Math.max(1, Math.abs(endpoint - zero))), height: 14, rx: 2, class: className,
        'data-chart-point': 'true', 'data-chart-series': className, 'data-chart-label': caption,
        'data-chart-detail': `${outcome === 'yes' ? 'BUY YES / long YES' : 'BUY NO / short YES'} | ${caption} | ${(edge * 100).toFixed(4)} pp`},
        tag('title', {}, escape(`${outcome === 'yes' ? 'BUY YES / long YES' : 'BUY NO / short YES'}: ${caption} edge ${(edge * 100).toFixed(4)} percentage points`)));
      body += text(WIDTH - right + 10, y + 11, `${edge >= 0 ? '+' : ''}${(edge * 100).toFixed(2)} pp`, {class: 'chart-label'});
      if (threshold !== null) body += line(x(threshold), y - 3, x(threshold), y + 17, 'strategy-threshold-entry');
    }
  }
  body += text(left, 270, openingThreshold === null ? 'Entry threshold unavailable: complete policy is required.'
    : `Required: ${(openingThreshold * 100).toFixed(2)} pp after entry fee; ${(roundTripThreshold * 100).toFixed(2)} pp after both fees.`, {class: 'strategy-chart-note'});
  return svg(opts.title, body);
}

function renderPortfolioChart(rawPoints, rawOptions = {}) {
  const opts = options(rawOptions, 'Observed shared paper cash, marked equity and cost-basis equity');
  const allRows = samples(rawPoints, [['cash', 0, 1e12], ['equity', 0, 1e12], ['costBasis', 0, 1e12]]), rows = windowRows(allRows, opts);
  const rawTrades = opts.trades === undefined ? [] : opts.trades;
  if (!Array.isArray(rawTrades) || rawTrades.length > 1000) fail('Invalid strategy chart execution history.');
  const trades = rawTrades.map(row => {
    if (!object(row) || !['buy', 'sell', 'flip', 'settle'].includes(row.action)) fail('Invalid strategy chart execution.');
    return {time: number(row.time, 0, MAX_TIME), action: row.action,
      outcome: row.outcome === undefined || row.outcome === null ? null : ['yes', 'no'].includes(row.outcome) ? row.outcome : fail('Invalid execution outcome.'),
      count: row.count === undefined || row.count === null ? null : number(row.count, 0, 1e12)};
  });
  const values = rows.flatMap(row => ['cash', 'equity', 'costBasis'].flatMap(key => row[key] === null ? [] : [row[key]]));
  if (!values.length) return empty(opts.title, allRows.length && !rows.length ? 'No observed samples in this window.' : 'Waiting for observed paper portfolio snapshots.', opts);
  const minimum = Math.min(...values), maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * .12, Math.max(maximum * .01, .25));
  const chart = frame(rows, Math.max(0, minimum - padding), maximum + padding,
    'Paper ledger value (USD; auto-scaled)', value => `$${value.toLocaleString('en-US', maximum >= 1e6
      ? {notation: 'compact', maximumFractionDigits: 1} : {maximumFractionDigits: maximum < 1000 ? 2 : 0})}`, opts);
  let body = chart.body;
  body += series(rows, 'costBasis', 'strategy-line-basis', chart, opts);
  body += series(rows, 'cash', 'strategy-line-cash', chart, opts);
  body += series(rows, 'equity', 'strategy-line-equity', chart, opts);
  for (const trade of trades.filter(row => row.time >= chart.xmin && row.time <= chart.xmax)) {
    const at = chart.x(trade.time), y = HEIGHT - FRAME.bottom - 3;
    const className = trade.action === 'buy' ? 'strategy-trade-buy' : trade.action === 'sell' ? 'strategy-trade-sell' : 'strategy-trade-settle';
    const title = `${trade.action.toUpperCase()}${trade.outcome ? ` ${trade.outcome.toUpperCase()}` : ''}${trade.count === null ? '' : ` ${trade.count} contracts`}; execution time only`;
    body += tag('path', {d: `M${fixed(at)},${fixed(y - 8)} L${fixed(at - 4)},${fixed(y)} L${fixed(at + 4)},${fixed(y)} Z`, class: className,
      'data-chart-point': 'true', 'data-chart-series': 'executions', 'data-chart-label': 'Recorded paper executions',
      'data-chart-detail': `${title} | ${new Date(trade.time * 1000).toISOString()}`}, tag('title', {}, escape(title)));
  }
  return svg(opts.title, body, opts);
}

  return Object.freeze({renderProbabilityChart, renderRollingChart, renderEdgeChart, renderPortfolioChart});
})();
export const renderStrategyProbabilityChart = strategyChartGeometry.renderProbabilityChart;
export const renderStrategyRollingChart = strategyChartGeometry.renderRollingChart;
export const renderStrategyEdgeChart = strategyChartGeometry.renderEdgeChart;
export const renderStrategyPortfolioChart = strategyChartGeometry.renderPortfolioChart;
