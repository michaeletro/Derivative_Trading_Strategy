"""Read-only native settlement-grid reconciliation; never selects live rules.

Candidate grids are fixed before looking at outcomes. Official labels and
expiration values remain unchanged. Rounded halfway values retain both
possible nearest values; no undocumented tie policy is invented.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
import hashlib
import json
import math
from pathlib import Path
import statistics

from .crypto_assets import asset_spec
from .forecast import _timestamp
from .historical_settlement_payoff import _terms
from .history_features import _contracts, _dataset
from .stochastic_features import _read_only


GRIDS = (
    dict(name='preclose_phase_000ms', start_offset_ms=-60000, step_ms=1000, samples=60,
         interval='[T-60,T)', phase_ms=0),
    dict(name='legacy_through_close', start_offset_ms=-59000, step_ms=1000, samples=60,
         interval='(T-60,T]', phase_ms=0),
    *(dict(name='preclose_phase_'+str(phase).zfill(3)+'ms', start_offset_ms=-60000+phase,
           step_ms=1000, samples=60, interval='[T-60,T)', phase_ms=phase)
      for phase in (200, 400, 600, 800)),
)


class SettlementReconciliationError(ValueError):
    """A native archive, contract identity or bounded audit input is invalid."""


def _decimal(value):
    try:
        if type(value) not in (str, int, float, Decimal):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise SettlementReconciliationError('A native price must be finite and positive') from None


def _rounded_values(average, digits):
    tick = Decimal(1).scaleb(-digits)
    scaled = average/tick
    lower = scaled.to_integral_value(rounding=ROUND_FLOOR)
    remainder = scaled-lower
    integers = (lower, lower+1) if remainder==Decimal('.5') else (
        (lower,) if remainder<Decimal('.5') else (lower+1,))
    return [format((value*tick).quantize(tick), 'f') for value in integers]


def audit_contract(market, label, *, asset, lookup):
    """Audit all predeclared grids using an exact millisecond sample lookup."""
    profile = asset_spec(asset)
    close = _timestamp(market['close_time'])
    if close != int(close) or type(label.get('binary_y')) is not int or label['binary_y'] not in (0, 1):
        raise SettlementReconciliationError('An integer-second close and official binary label are required')
    if label.get('result') not in ('yes','no') or market.get('result') != label.get('result') or label['binary_y'] != int(label['result']=='yes'):
        raise SettlementReconciliationError('Native official outcome fields disagree')
    digits = _terms(market, profile)
    strike, tick = _decimal(market['floor_strike']), Decimal(1).scaleb(-digits)
    if market.get('strike_type') not in ('greater', 'greater_or_equal'):
        raise SettlementReconciliationError('Unsupported native comparator')
    threshold = strike+(tick/2 if market['strike_type']=='greater' else -tick/2)
    venue = None
    if market.get('expiration_value') not in (None, ''):
        venue = _decimal(market['expiration_value'])
    output = dict(ticker=market['ticker'], close_time=market['close_time'],
        nominal_strike=str(strike), comparator=market['strike_type'], round_digits=digits,
        effective_strike=str(threshold), official_result=label['result'], official_y=label['binary_y'],
        official_label_received_at=label['received_at'], venue_expiration_value=None if venue is None else str(venue),
        candidate_grids={}, original_labels_unchanged=True)
    for grid in GRIDS:
        stamps = [int(close*1000)+grid['start_offset_ms']+index*grid['step_ms'] for index in range(grid['samples'])]
        samples = [lookup(stamp) for stamp in stamps]
        unavailable = Counter(sample['status'] for sample in samples if sample['status'] != 'ready')
        result = dict(status='unavailable' if unavailable else 'complete', count_samples=60,
            unavailable_samples=dict(unavailable), average=None, rounded_value_candidates=None,
            nearest_rounding_halfway=None, predicted_y=None, outcome_halfway_ambiguous=None,
            official_y_disagrees=None, venue_value_rounding_match=None, raw_minus_venue=None)
        if not unavailable:
            with localcontext() as context:
                context.prec = 60
                total = sum((_decimal(sample['value']) for sample in samples), Decimal(0))
                average = total/60
                values = _rounded_values(average, digits)
                centered = total-60*threshold
            predicted = None if centered==0 else int(centered>0)
            result.update(average=str(average), rounded_value_candidates=values,
                nearest_rounding_halfway=len(values)>1, predicted_y=predicted,
                outcome_halfway_ambiguous=predicted is None,
                official_y_disagrees=None if predicted is None else predicted!=label['binary_y'],
                venue_value_rounding_match=None if venue is None else venue in [Decimal(value) for value in values],
                raw_minus_venue=None if venue is None else str(average-venue))
        output['candidate_grids'][grid['name']] = result
    legacy, preclose = (output['candidate_grids'][name] for name in ('legacy_through_close', 'preclose_phase_000ms'))
    if legacy['official_y_disagrees'] is True and preclose['official_y_disagrees'] is False and preclose['venue_value_rounding_match'] is True:
        reason = 'one_second_endpoint_explains_official_outcome_and_reported_value'
    elif legacy['status']=='unavailable' or preclose['status']=='unavailable':
        reason = 'source_samples_missing_flagged_or_conflicting'
    elif legacy['outcome_halfway_ambiguous'] or preclose['outcome_halfway_ambiguous']:
        reason = 'unspecified_rounding_halfway_can_affect_outcome'
    elif legacy['official_y_disagrees'] is True:
        reason = 'unresolved_official_outcome_difference'
    else:
        reason = 'no_legacy_binary_outcome_disagreement'
    output['classification'] = reason
    return output


class _NativeWindow:
    def __init__(self, db, profile):
        self.db, self.profile = db, profile
        rows = db.execute('''SELECT a.id,a.hour,a.status,a.started_at,a.received_at,a.payload_sha256,
            (SELECT COUNT(*) FROM history_acquisitions b WHERE b.index_id=a.index_id AND b.hour=a.hour) AS archived_versions
            FROM history_acquisitions a JOIN
            (SELECT hour,MAX(id) AS latest FROM history_acquisitions WHERE index_id=? GROUP BY hour) b
            ON a.id=b.latest ORDER BY a.hour''', (profile.index_id,)).fetchall()
        if len(rows)>744:
            raise SettlementReconciliationError('The native settlement audit is bounded to 744 hours per asset')
        self.acquisitions = {int(_timestamp(row['hour'])):dict(row) for row in rows}
        self.used = {}

    def window(self, close):
        left, right = int(close*1000)-60000, int(close*1000)
        native = defaultdict(list)
        first, last = (left//3600000-1)*3600, right//3600000*3600
        for hour in range(first, last+1, 3600):
            acquisition = self.acquisitions.get(hour)
            if acquisition is None:
                continue
            self.used[acquisition['id']] = acquisition
            if acquisition['status']=='error':
                continue
            for row in self.db.execute('''SELECT source_time_ms,value,repeated,amendment FROM history_ticks
                    WHERE acquisition_id=? AND source_time_ms>=? AND source_time_ms<=?
                    ORDER BY source_time_ms''', (acquisition['id'],left,right)):
                native[row['source_time_ms']].append(dict(row, acquisition_id=acquisition['id'],
                    native_timestamp_valid=type(row['source_time_ms']) is int
                    and hour*1000<=row['source_time_ms']<=(hour+3600)*1000))
        def lookup(stamp):
            variants = native.get(stamp, [])
            owner = self.acquisitions.get(stamp//3600000*3600)
            status = 'ready'
            values = []
            if owner is not None and owner['status']=='error':
                status = 'latest_hour_acquisition_failed'
            elif not variants:
                status = 'missing_exact_native_tick'
            else:
                for row in variants:
                    if not row['native_timestamp_valid']:
                        status = 'invalid_native_timestamp'
                    try:
                        values.append(_decimal(row['value']))
                    except ValueError:
                        status = 'invalid_native_value'
                    if row['repeated'] or row['amendment'] is not None:
                        status = 'flagged_native_tick'
                if len(set(values))>1:
                    status = 'conflicting_adjacent_acquisition_tick'
            return dict(event_time_ms=stamp, status=status,
                value=str(values[0]) if values and status=='ready' else None,
                acquisition_ids=[row['acquisition_id'] for row in variants],
                repeated=any(row['repeated'] for row in variants),
                amendment=any(row['amendment'] is not None for row in variants),
                original_variants=variants)
        return lookup

    def integrity(self, identifiers):
        result = []
        for identifier in sorted(identifiers):
            acquisition = self.used[identifier]
            row = self.db.execute('SELECT payload FROM history_acquisitions WHERE id=?', (identifier,)).fetchone()
            payload = row['payload']
            verified = isinstance(payload,str) and hashlib.sha256(payload.encode()).hexdigest()==acquisition['payload_sha256']
            result.append(dict(acquisition, original_payload_sha256_rechecked=verified,
                source_revisions_absent_proven=False))
        return result


def _summary(contracts, name):
    values = [row['candidate_grids'][name] for row in contracts]
    errors = sorted(abs(float(value['raw_minus_venue'])) for value in values if value['raw_minus_venue'] is not None)
    return dict(contracts=len(values), complete_grids=sum(row['status']=='complete' for row in values),
        unavailable_grids=sum(row['status']!='complete' for row in values),
        official_binary_disagreements=sum(row['official_y_disagrees'] is True for row in values),
        binary_halfway_ambiguous=sum(row['outcome_halfway_ambiguous'] is True for row in values),
        nearest_rounding_halfway=sum(row['nearest_rounding_halfway'] is True for row in values),
        venue_values_compared=sum(row['venue_value_rounding_match'] is not None for row in values),
        venue_rounded_matches=sum(row['venue_value_rounding_match'] is True for row in values),
        venue_uniquely_rounded_matches=sum(row['venue_value_rounding_match'] is True and not row['nearest_rounding_halfway'] for row in values),
        venue_matches_compatible_with_unspecified_halfway=sum(row['venue_value_rounding_match'] is True and row['nearest_rounding_halfway'] for row in values),
        venue_rounded_mismatches=sum(row['venue_value_rounding_match'] is False for row in values),
        mean_absolute_raw_minus_venue=None if not errors else math.fsum(errors)/len(errors),
        median_absolute_raw_minus_venue=None if not errors else statistics.median(errors),
        max_absolute_raw_minus_venue=None if not errors else errors[-1])


def reconcile_settlements(bindings, *, feature_rows=None, assets=None, maximum_details=100):
    """Audit every eligible native market in one exact dataset per bound asset."""
    if not isinstance(bindings,dict) or not 1<=len(bindings)<=4:
        raise SettlementReconciliationError('Supply one through four explicit asset archive bindings')
    selected = tuple(assets or bindings)
    if not selected or len(selected)!=len(set(selected)) or not set(selected)<=set(bindings):
        raise SettlementReconciliationError('Choose each bound audit asset once')
    if type(maximum_details) is not int or not 1<=maximum_details<=1000:
        raise SettlementReconciliationError('Exception details must be bounded to one through 1000 contracts')
    features = {}
    if feature_rows is not None:
        if not isinstance(feature_rows,(list,tuple)) or len(feature_rows)>200000:
            raise SettlementReconciliationError('Feature references must be a bounded row sequence')
        for row in feature_rows:
            key = row['asset'],row['ticker']
            previous = features.get(key)
            identity = lambda value:(value.get('close_time'),_decimal(value.get('target_price')),
                value.get('strike_type'),value.get('y'),value.get('label_available_at'),value.get('source'))
            if previous is not None and (identity(row)!=identity(previous) or row['native_reference']!=previous['native_reference']):
                raise SettlementReconciliationError('Feature references disagree on original native contract identity')
            features[key] = row
    report = dict(kind='kalshi_settlement_reconciliation_report',version=1,
        research_only=True,execution_eligible=False,live_model_updated=False,
        official_labels_modified=False,automatic_endpoint_promotion=False,
        predeclared_candidate_grids=[dict(grid) for grid in GRIDS],assets={},
        limitations=[
            'Grids are descriptive predeclared alternatives; this tool never updates a forecasting or trading configuration.',
            'Native expiration-value reconciliation across the full archive is separate from selecting on five failed labels.',
            'Nearest-decimal halfway cases retain both possible nearest values; no tie convention is invented.',
            'Historical acquisition and amendment flags cannot prove prices were identical to the original settlement-time feed.',
            'Official outcomes are retained regardless of reconstruction success; difficult contracts are not filtered from probability scoring.',
            'Stored acquisition hashes are rechecked for detailed exceptions only; all acquisition metadata remains explicit.'])
    for asset in selected:
        profile = asset_spec(asset)
        with _read_only(Path(bindings[asset]), 'history') as (db, location):
            identifiers = {row['native_reference']['contract_document_id'] for (symbol,_),row in features.items() if symbol==asset}
            if len(identifiers)>1:
                raise SettlementReconciliationError('Use one original native document per asset')
            document,dataset = _dataset(db,asset,next(iter(identifiers),None))
            for (symbol,_),row in features.items():
                if symbol==asset and row['native_reference']['contract_document_sha256']!=document['payload_sha256']:
                    raise SettlementReconciliationError('Feature original document hash differs from audit dataset')
            contracts, rejected = _contracts(dataset)
            native_contracts = {market['ticker']:(market,label,close) for market,label,close in contracts}
            for (symbol,ticker),feature in features.items():
                if symbol!=asset:
                    continue
                original = native_contracts.get(ticker)
                if original is None:
                    raise SettlementReconciliationError('A supplied feature ticker is absent from eligible original native contracts')
                market,label,close = original
                if (feature.get('close_time')!=close or _decimal(feature.get('target_price'))!=_decimal(market['floor_strike'])
                        or feature.get('strike_type')!=market['strike_type'] or type(feature.get('y')) is not int
                        or feature['y']!=label['binary_y'] or feature.get('label_available_at')!=label['received_at']
                        or feature.get('source')!=profile.source):
                    raise SettlementReconciliationError('A supplied feature identity, official label or receipt differs from its original native contract')
            native, audited, errors, details = _NativeWindow(db,profile), [], Counter(), []
            for market,label,close in contracts:
                lookup = native.window(close)
                try:
                    result = audit_contract(market,label,asset=asset,lookup=lookup)
                except ValueError as error:
                    errors[str(error)] += 1
                    continue
                audited.append(result)
                legacy, preclose = (result['candidate_grids'][name] for name in ('legacy_through_close','preclose_phase_000ms'))
                exceptional = (legacy['official_y_disagrees'] is True or legacy['status']!='complete'
                    or preclose['status']!='complete' or preclose['venue_value_rounding_match'] is False)
                if exceptional:
                    errors['detailed_exception_contracts'] += 1
                    if len(details)<maximum_details:
                        result['sixty_tick_samples'] = {grid['name']:[lookup(int(close*1000)+grid['start_offset_ms']+i*1000)
                            for i in range(60)] for grid in GRIDS[:2]}
                        ids = {identifier for samples in result['sixty_tick_samples'].values()
                               for sample in samples for identifier in sample['acquisition_ids']}
                        result['original_acquisition_integrity'] = native.integrity(ids)
                        result['native_contract_document'] = dict(id=document['id'],sha256=document['payload_sha256'],
                            received_at=document['received_at'],original_primary_rules=market.get('rules_primary'),
                            original_secondary_rules=market.get('rules_secondary'))
                        feature = features.get((asset,market['ticker']))
                        if feature is not None:
                            result['prior_feature_reference'] = dict(native_reference=feature['native_reference'],
                                original_reconstructed_average=feature.get('realized_average'),
                                original_y=feature['y'],original_label_available_at=feature.get('label_available_at'))
                        details.append(result)
            summaries = {grid['name']:_summary(audited,grid['name']) for grid in GRIDS}
            report['assets'][asset] = dict(status='audited' if audited else 'unavailable',archive_path=str(location),
                native_contract_document=dict(id=document['id'],sha256=document['payload_sha256'],received_at=document['received_at']),
                eligible_native_contracts=len(contracts),audited_contracts=len(audited),
                complete_common_contracts=sum(all(value['status']=='complete' for value in row['candidate_grids'].values()) for row in audited),
                original_contract_exclusions=dict(rejected),audit_diagnostics=dict(errors),candidate_scores=summaries,
                native_acquisition_metadata=list(native.used.values()),
                original_acquisition_metadata_sha256=hashlib.sha256(json.dumps(list(native.used.values()),sort_keys=True,
                    separators=(',',':'),allow_nan=False).encode()).hexdigest(),
                archived_hours_with_multiple_versions=sum(row['archived_versions']>1 for row in native.used.values()),
                exception_details=details,exception_details_truncated=errors['detailed_exception_contracts']>len(details),
                endpoint_explains_all_legacy_label_disagreements=all(
                    row['classification']=='one_second_endpoint_explains_official_outcome_and_reported_value'
                    for row in audited if row['candidate_grids']['legacy_through_close']['official_y_disagrees'] is True),
                preclose_grid_matches_every_compared_venue_value=(
                    summaries['preclose_phase_000ms']['venue_values_compared']>0
                    and summaries['preclose_phase_000ms']['venue_rounded_mismatches']==0),
                official_labels_modified=False,source_revisions_absent_proven=False)
    return report


def demo_reconciliation():
    """Synthetic endpoint plumbing only; not native evidence or model research."""
    from datetime import datetime,timezone
    close = 1800000000
    market = dict(ticker='KXBTC15M-SYNTHETIC-AUDIT',close_time=datetime.fromtimestamp(close,timezone.utc).isoformat(),
        floor_strike='100.00',strike_type='greater_or_equal',result='no',expiration_value='99.99',
        rules_primary="The simple average of the sixty seconds of CF Benchmarks' BRTI before close applies.",
        rules_secondary='The final value is rounded to the nearest 2 decimal places.',custom_strike=dict(round_digits='2'))
    label = dict(result='no',binary_y=0,received_at=close+1)
    def lookup(stamp):
        return dict(event_time_ms=stamp,status='ready',value='100.60' if stamp==close*1000 else '99.99')
    result = audit_contract(market,label,asset='BTC',lookup=lookup)
    return dict(kind='kalshi_settlement_reconciliation_report',version=1,synthetic=True,
        research_only=True,execution_eligible=False,live_model_updated=False,official_labels_modified=False,
        automatic_endpoint_promotion=False,predeclared_candidate_grids=[dict(grid) for grid in GRIDS],
        assets=dict(BTC=dict(status='synthetic_demo',candidate_scores={grid['name']:_summary([result],grid['name']) for grid in GRIDS},
                             exception_details=[result])),
        limitations=['Synthetic endpoint plumbing; no native observations or official venue records are represented.'])
