// Input/record validation only. All prices and estimates come from the C++ engine.
export const ENGINE = 'european-gbm-v1';
export const KIND = 'derivative_lab.pricing_experiment';
export const FIELDS = ['schema_version','exercise_style','right','spot','strike','maturity_years','rate','dividend_yield','volatility','paths','seed','method','currency'];
const finite = x => typeof x === 'number' && Number.isFinite(x);
function bounded(x, low, high, name) {
  if (!finite(x) || x < low || x > high) throw new Error(`${name} must be between ${low} and ${high}.`);
}
export function validateRequest(input) {
  if (!input || Array.isArray(input) || Object.keys(input).length !== FIELDS.length || FIELDS.some(k => !Object.hasOwn(input,k)))
    throw new Error('Experiment has missing or unexpected input fields.');
  if (input.schema_version !== 1 || input.exercise_style !== 'european') throw new Error('Only schema 1 European experiments are supported.');
  if (!['call','put'].includes(input.right) || !['plain','antithetic'].includes(input.method)) throw new Error('Choose a supported right and simulation method.');
  bounded(input.spot,0,1e8,'Spot'); bounded(input.strike,1e-8,1e8,'Strike');
  bounded(input.maturity_years,0,30,'Maturity'); bounded(input.rate,-.5,.5,'Rate');
  bounded(input.dividend_yield,-.5,.5,'Yield'); bounded(input.volatility,0,3,'Volatility');
  if (input.volatility*Math.sqrt(input.maturity_years)>3) throw new Error('Total log-return standard deviation must not exceed 3.');
  if (!Number.isInteger(input.paths) || input.paths<1000 || input.paths>2000000 || (input.method==='antithetic' && input.paths%2))
    throw new Error('Use 1,000–2,000,000 paths; antithetic mode requires an even number.');
  if (typeof input.seed !== 'string' || !/^[0-9]{1,20}$/.test(input.seed) || BigInt(input.seed)>18446744073709551615n)
    throw new Error('Seed must be an unsigned 64-bit integer written as decimal digits.');
  if (typeof input.currency !== 'string' || !/^[A-Z]{3}$/.test(input.currency)) throw new Error('Currency label must be three uppercase letters.');
  const result=Object.fromEntries(FIELDS.map(k=>[k,input[k]]));
  result.seed=BigInt(input.seed).toString();
  return result;
}
export function requestFromForm(form) {
  const values=Object.fromEntries(FIELDS.map(k=>[k,form.get(k)]));
  values.schema_version=1; values.exercise_style='european';
  for (const name of ['spot','strike','maturity_years','rate','dividend_yield','volatility','paths']) {
    if (typeof values[name] !== 'string' || !values[name].trim()) throw new Error(`Supply ${name}.`);
    values[name]=Number(values[name]);
  }
  values.seed=String(values.seed??'').trim(); values.currency=String(values.currency??'').trim().toUpperCase();
  return validateRequest(values);
}
export function importRequest(text) {
  if (typeof text !== 'string' || text.length>131072) throw new Error('Experiment file exceeds 128 KiB.');
  let record;
  try { record=JSON.parse(text); } catch { throw new Error('Experiment file is not valid JSON.'); }
  if (record?.kind!==KIND || record.schema_version!==1 || record.model?.engine_version!==ENGINE)
    throw new Error('Not a supported Derivative Lab experiment record.');
  // Never trust or render stored results: only restore validated inputs for a new run.
  return validateRequest(record.request);
}
export function validateRecord(record, submitted) {
  if (record?.kind!==KIND || record.schema_version!==1 || record.model?.engine_version!==ENGINE ||
      record.model?.input_source!=='manual_scenario_not_broker_data' || record.model?.scheme!=='exact_terminal_gbm')
    throw new Error('Unexpected pricing-engine response.');
  const request=validateRequest(record.request);
  const expected=validateRequest(submitted);
  if (FIELDS.some(k=>request[k]!==expected[k])) throw new Error('Pricing response does not match submitted inputs.');
  const r=record.result, mc=r?.monte_carlo;
  const validEstimate=e=>e && finite(e.price) && e.price>=0 && finite(e.sample_variance) && e.sample_variance>=0 &&
    ((e.standard_error===null && e.ci_low===null && e.ci_high===null) ||
      (finite(e.standard_error) && e.standard_error>=0 && finite(e.ci_low) && finite(e.ci_high) && e.ci_low<=e.price && e.ci_high>=e.price));
  if (!validEstimate(mc) || !finite(r.analytical_price) || r.analytical_price<0 || !finite(r.runtime_ms) || r.runtime_ms<0 ||
      typeof r.deterministic!=='boolean' || !Array.isArray(r.convergence) || r.convergence.length>30 ||
      !Array.isArray(r.warnings) || r.warnings.length>10 || r.warnings.some(w=>typeof w!=='string'||w.length>500) ||
      typeof record.build?.pricing_source_sha256!=='string' || !/^[a-f0-9]{64}$/.test(record.build.pricing_source_sha256))
    throw new Error('Invalid numerical results; no experiment was accepted.');
  const deterministic=request.spot===0 || request.maturity_years===0 || request.volatility===0;
  const samples=request.paths/(request.method==='antithetic'?2:1);
  if (r.deterministic!==deterministic || r.paths_evaluated!==(deterministic?0:request.paths) ||
      r.independent_samples!==(deterministic?0:samples) || !Number.isInteger(r.positive_payoffs) ||
      r.positive_payoffs<0 || r.positive_payoffs>r.paths_evaluated)
    throw new Error('Simulation sample counts do not match the experiment.');
  let previous=0;
  for (const p of r.convergence) {
    if (!validEstimate(p) || !Number.isInteger(p.paths) || p.paths<=previous || p.paths>request.paths ||
        p.independent_samples!==p.paths/(request.method==='antithetic'?2:1)) throw new Error('Invalid convergence series.');
    previous=p.paths;
  }
  if (!deterministic && (previous!==request.paths || r.convergence.at(-1).price!==mc.price)) throw new Error('Incomplete convergence series.');
  return record;
}
export const valueText=x=>finite(x)?new Intl.NumberFormat('en-US',{maximumSignificantDigits:9}).format(x):'—';
