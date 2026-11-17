"""Recover causal book and source evidence from verified, closed research captures.

Recovery never synthesizes forecasts, moves original clocks, or expands the
provided forecast-contract universe. Captured books can be useful before the
next model calculation, but mirrored representations share one liquidity token.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from urllib.parse import unquote, urlsplit

from .btc15m import _effective_fee, verified_crypto_settlement_source
from .crypto_assets import ASSETS, asset_from_ticker, asset_spec
from .engine import _timestamp
from .minute_five import _identity
from .pacing_scenario_archives import _recorded_quantum
from .research_analysis import ResearchAnalysisError, ResearchReader
from .scheduled_research import validate_records
from .value_replay import _levels
from .value_sizing import ValueSizingConfig


MAX_EVENTS = 200_000
OBSERVATIONS = ('kalshi_scheduled_observation', 'kalshi_value_observation')
SETTLEMENTS = ('kalshi_scheduled_settlement', 'kalshi_value_settlement')
BOOKS = ('kalshi_scheduled_book', 'kalshi_value_book')
GAPS = ('kalshi_scheduled_book_gap', 'kalshi_value_book_gap')
_TOKEN = object()


class PacingRecoveryError(ValueError):
    """Invalid, incomplete, or contradictory captured recovery evidence."""


class _VerifiedRecoveryBundle(dict):
    """In-process result of the verified reader, not a portable trust flag."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._verification_token = _TOKEN


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PacingRecoveryError('Recovery clocks must be finite positive epoch numbers')
    return float(value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _fixed(value):
    text = format(Decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _book(yes, no):
    yes, no = _levels(yes), _levels(no)
    if not yes or not no or Decimal(yes[-1][0])+Decimal(no[-1][0]) >= 1:
        raise PacingRecoveryError('Recovery requires an unlocked two-sided recorded book')
    normalized = [[[ _fixed(price), _fixed(quantity)] for price, quantity in side] for side in (yes, no)]
    return yes, no, _hash(normalized)


def _endpoint(path):
    if not isinstance(path, str) or len(path) > 4096:
        return None
    path = urlsplit(path).path
    path = re.sub(r'^/trade-api/v2', '', path)
    match = re.fullmatch(r'/(?:historical/)?markets/([^/]+)/orderbook', path)
    if match:
        return 'book', unquote(match.group(1))
    match = re.fullmatch(r'/(markets|series|events)/([^/]+)', path)
    if match:
        return {'markets':'market', 'series':'series', 'events':'event'}[match.group(1)], unquote(match.group(2))
    if path == '/markets':
        return 'markets', None
    return None


def read_recovery_events(directory, *, run_id=None, max_events=MAX_EVENTS):
    """Verify whole producer snapshots before exposing selected response bodies.

    All selected producers are verified before asset filtering. No payload from
    an invalid later row, unclosed producer, sequence loss, or unmatched request
    attempt is allowed to become recovery evidence.
    """
    if type(max_events) is not int or not 1 <= max_events <= 1_000_000:
        raise PacingRecoveryError('A bounded capture scan is required')
    try:
        reader = ResearchReader(directory, run_id=run_id, max_events=max_events)
        selected, starts, ends = [], {}, {}
        for event in reader.events():
            payload = event.get('payload')
            if not isinstance(payload, dict):
                continue
            if event['kind'] in ('request_attempt_start', 'request_attempt_end'):
                request = event.get('request_id') or payload.get('request_id')
                attempt = payload.get('attempt_id', payload.get('attempt_index'))
                key = (event['run_id'], event['producer_id'], request, attempt)
                if not isinstance(request, str) or not isinstance(attempt, (str, int)) or isinstance(attempt, bool):
                    raise PacingRecoveryError('Captured request attempt identity is incomplete')
                identity = (payload.get('method'), payload.get('path'), payload.get('started_at'))
                paired = starts if event['kind'] == 'request_attempt_start' else ends
                if key in paired and paired[key] != identity:
                    raise PacingRecoveryError('Captured request attempt mirrors disagree')
                paired[key] = identity
                if event['kind'] == 'request_attempt_end' and _endpoint(payload.get('path')) is not None:
                    selected.append(event)
        coverage = reader.coverage()
        if not coverage['complete'] or starts != ends:
            raise PacingRecoveryError('Recovery requires closed, loss-free, untruncated captures with matched request attempts')
        return _VerifiedRecoveryBundle(events=selected, coverage=coverage,
            verification='ResearchReader canonical payload hashes, sequence, producer health and request attempt pairs',
            synthetic=any((run.get('metadata') or {}).get('synthetic') is True for run in reader.runs))
    except ResearchAnalysisError as error:
        raise PacingRecoveryError(str(error)) from None


def _facts(bundle, counts, exclusions):
    values, attempts = [], {}
    for event in bundle['events']:
        payload = event.get('payload')
        if not isinstance(payload, dict) or event.get('kind') != 'request_attempt_end':
            continue
        endpoint = _endpoint(payload.get('path'))
        if endpoint is None:
            continue
        counts['candidate_response_events'] += 1
        if (payload.get('method') != 'GET' or payload.get('outcome') != 'completed'
                or payload.get('transport_attempted') is not True or type(payload.get('http_status')) is not int
                or not 200 <= payload['http_status'] < 300 or not isinstance(payload.get('response'), dict)):
            exclusions['unsuccessful_or_unrecorded_response'] += 1
            continue
        try:
            started = _time(payload.get('started_at'))
            quote = _time(payload.get('completed_at'))
            captured = _time(event.get('recorded_at'))
            if started > quote:
                raise PacingRecoveryError('Captured request clocks disagree')
            visible = max(quote, captured)
            request, attempt = payload.get('request_id') or event.get('request_id'), payload.get('attempt_id')
            if (not isinstance(request, str) or not request or not isinstance(attempt, str) or not attempt
                    or not isinstance(event.get('event_id'), str) or not isinstance(event.get('run_id'), str)):
                raise PacingRecoveryError('Captured response identity is unavailable')
            signature = _hash(dict(endpoint=endpoint, started=started, quote=quote, response=payload['response']))
            key = event['run_id'], attempt
            proof = dict(event_id=event['event_id'], run_id=event['run_id'],
                producer_id=event.get('producer_id'), request_id=request, attempt_id=attempt,
                payload_sha256=event.get('payload_sha256'), request_started_at=started,
                quote_received_at=quote, capture_recorded_at=captured, visible_at=visible)
            fact = dict(kind=endpoint[0], identifier=endpoint[1], body=payload['response'],
                started=started, quote=quote, visible=visible, proof=proof,
                token='capture-book-'+_hash([event['run_id'], attempt]))
            previous = attempts.get(key)
            if previous is not None:
                if previous['signature'] != signature:
                    raise PacingRecoveryError('Mirrored request attempts contain contradictory response evidence')
                counts['mirrored_response_events'] += 1
                if visible < previous['fact']['visible']:
                    previous['fact'].update(fact)
                continue
            attempts[key] = dict(signature=signature, fact=fact)
            values.append(fact)
        except (KeyError, TypeError) as error:
            raise PacingRecoveryError('Captured response clocks or identity are invalid') from error
    return sorted(values, key=lambda row: (row['visible'], row['quote'], row['proof']['event_id']))


def _source_proof(market, asset, series, event, portable=None):
    if (not isinstance(series, dict) or not isinstance(event, dict)
            or series.get('ticker') != asset_spec(asset).series
            or event.get('event_ticker') != market.get('event_ticker')
            or event.get('series_ticker') != asset_spec(asset).series):
        raise PacingRecoveryError('Captured source documents do not bind the exact market and asset')
    verified = verified_crypto_settlement_source(series, market, event, asset=asset)
    fee_type, multiplier = _effective_fee(series, event)
    if portable is not None:
        if portable.get('fee_type') != fee_type:
            raise PacingRecoveryError('Latest captured source fees conflict with the original forecast')
        recorded = ValueSizingConfig(fee_type=fee_type,
            fee_multiplier=portable.get('fee_multiplier')).fee_multiplier
        if recorded != multiplier:
            raise PacingRecoveryError('Latest captured source fees conflict with the original forecast')
    return verified, fee_type, str(multiplier)


def _aligned_market(market, identity):
    """Reject economic conflicts; exclude unpublished identity components.

    Exact Decimal values and UTC instants may have different JSON spellings.
    Only the merged view adopts the original identity spelling; the captured
    payload and its hash remain unchanged.
    """
    missing, changed_spelling = False, []
    for key, expected in identity.items():
        actual = market.get(key)
        if actual is None:
            if key == 'cap_strike' and expected is None:
                continue
            missing = True
            continue
        try:
            if key in ('floor_strike', 'notional_value_dollars'):
                left, right = Decimal(str(actual)), Decimal(str(expected))
                same = not isinstance(actual, bool) and left.is_finite() and right.is_finite() and left == right
            elif key in ('open_time', 'close_time'):
                same = _timestamp(actual) == _timestamp(expected)
            else:
                same = actual == expected
        except (ValueError, TypeError, InvalidOperation):
            same = False
        if not same:
            raise PacingRecoveryError('Captured market metadata conflicts with the original payout identity')
        if actual != expected:
            changed_spelling.append(key)
    if missing:
        return None, []
    return dict(market, **identity), changed_spelling


def _attach_certificate(row, series_docs, event_docs, counts, exclusions):
    market, asset = row.get('market'), row['asset']
    if not isinstance(market, dict):
        return row
    meta = market.get('_crypto15m')
    if isinstance(meta, dict) and meta.get('settlement_source_evidence') is not None:
        return row
    legacy = market.get('_btc15m')
    evidence = legacy.get('settlement_source_evidence') if isinstance(legacy, dict) else None
    legacy_docs = None
    if isinstance(evidence, dict):
        legacy_docs = (dict(value=evidence.get('series'), visible=row['observed_at'],
                     proof={'location':'original_market._btc15m.settlement_source_evidence'}),
                dict(value=evidence.get('event'), visible=row['observed_at'],
                     proof={'location':'original_market._btc15m.settlement_source_evidence'}))
    # A newer captured component cannot be bypassed by an older convenient
    # legacy certificate. The untouched legacy component remains usable only
    # when no independently captured replacement was received.
    series = series_docs.get(asset_spec(asset).series)
    event = event_docs.get(market.get('event_ticker'))
    series = legacy_docs[0] if series is None and legacy_docs is not None else series
    event = legacy_docs[1] if event is None and legacy_docs is not None else event
    docs = (series, event) if series is not None and event is not None else None
    if docs is None:
        exclusions['forecast_source_documents_not_previously_recorded'] += 1
        return row
    try:
        series, event = docs
        received = max(series['visible'], event['visible'])
        if received > row['observed_at']:
            raise PacingRecoveryError('Source documents were received after the forecast')
        verified, fee_type, multiplier = _source_proof(market, asset, series['value'], event['value'], row)
        if meta is not None and not isinstance(meta, dict):
            raise PacingRecoveryError('Original source metadata is malformed')
        new_meta = dict(meta or {})
        for key, value in verified.items():
            if key in new_meta and new_meta[key] != value:
                raise PacingRecoveryError('Captured source documents contradict existing market metadata')
            new_meta[key] = value
        for key, value in (('fee_type', fee_type), ('fee_multiplier', multiplier)):
            if key in new_meta and (new_meta[key] != value if key == 'fee_type'
                    else Decimal(str(new_meta[key])) != Decimal(value)):
                raise PacingRecoveryError('Captured source fees contradict existing market metadata')
            new_meta.setdefault(key, value)
        new_meta['settlement_source_evidence'] = dict(series=deepcopy(series['value']), event=deepcopy(event['value']))
        result = dict(row, market=dict(market, _crypto15m=new_meta))
        provenance = dict(row.get('recovery_provenance') or {})
        provenance['source_certificate'] = dict(received_at=received, series=deepcopy(series['proof']),
            event=deepcopy(event['proof']), source='causally_recorded_source_documents', original_market_unchanged=True)
        result['recovery_provenance'] = provenance
        counts['forecast_source_certificates_recovered'] += 1
        return result
    except (ValueError, TypeError, KeyError):
        exclusions['forecast_source_document_conflict_or_invalidity'] += 1
        return row


def recover_pacing_records(records, *, directory=None, capture_events=None, assets=None,
                           run_id=None, max_events=MAX_EVENTS):
    """Merge real recovery evidence without changing the original forecast universe.

    ``capture_events`` accepts only the in-process bundle produced by
    ``read_recovery_events``; a bare list or user-provided completeness flag is
    insufficient. Input records are copied only when their metadata changes.
    """
    if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
        raise PacingRecoveryError('A bounded recovery event limit is required')
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= max_events:
        raise PacingRecoveryError('Recovery requires a nonempty bounded original record list')
    if (directory is None) == (capture_events is None):
        raise PacingRecoveryError('Provide one verified capture directory or reader bundle')
    bundle = (read_recovery_events(directory, run_id=run_id, max_events=max_events)
        if directory is not None else capture_events)
    if (not isinstance(bundle, _VerifiedRecoveryBundle) or getattr(bundle, '_verification_token', None) is not _TOKEN
            or bundle.get('coverage', {}).get('complete') is not True):
        raise PacingRecoveryError('Recovery evidence must come from the verified closed-capture reader')
    selected = tuple(assets or sorted({row.get('asset') for row in records if isinstance(row, dict)
        and isinstance(row.get('asset'), str)}))
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(ASSETS):
        raise PacingRecoveryError('Select distinct supported recovery assets')
    copied, identities, universe, existing_outcomes = [], {}, set(), {}
    archive_books = defaultdict(list)
    previous = 0
    for original in records:
        if not isinstance(original, dict) or original.get('asset') not in selected:
            raise PacingRecoveryError('Every original recovery event must bind a selected asset')
        row = dict(original)
        kind = row.get('kind')
        if kind not in OBSERVATIONS+SETTLEMENTS+BOOKS+GAPS:
            raise PacingRecoveryError('Unsupported original recovery event kind')
        stamp = _time(row.get('available_at') if kind in SETTLEMENTS else row.get('observed_at'))
        if stamp < previous:
            raise PacingRecoveryError('Original records must retain receipt order')
        previous = stamp
        identity = row.get('contract_identity')
        if not isinstance(identity, dict) or identity.get('ticker') != row.get('ticker'):
            raise PacingRecoveryError('Original payout identity must bind its ticker')
        try:
            opened, closed = (_timestamp(identity[key]) for key in ('open_time', 'close_time'))
        except (ValueError, TypeError, KeyError):
            raise PacingRecoveryError('Original recovery contract clocks are unavailable') from None
        if abs(closed-opened-900) > 1e-6:
            raise PacingRecoveryError('Recovery supports exact fifteen-minute contracts')
        if identities.setdefault(row['ticker'], identity) != identity:
            raise PacingRecoveryError('An original contract changed its payout identity')
        if kind in OBSERVATIONS:
            universe.add(row['ticker'])
        if kind in OBSERVATIONS+BOOKS:
            _, _, body_hash = _book(row.get('yes_bids'), row.get('no_bids'))
            quote = _time(row.get('quote_time'))
            token = 'archive-book-'+_hash([row['ticker'], quote, body_hash])
            row.setdefault('book_evidence_id', token)
            row.setdefault('book_evidence_quote_time', quote)
            archive_books[row['ticker'], body_hash].append(row)
        elif kind in SETTLEMENTS:
            result = row.get('result')
            if result not in ('yes', 'no'):
                raise PacingRecoveryError('An original final outcome must be binary')
            old = existing_outcomes.get(row['ticker'])
            if old and old['result'] != result:
                raise PacingRecoveryError('Original authoritative outcomes conflict')
            if old is None or stamp < old['available_at']:
                existing_outcomes[row['ticker']] = row
        copied.append(row)
    counts, exclusions = Counter(), Counter()
    facts = _facts(bundle, counts, exclusions)
    # A wrapper quote interval can encompass worker and execution-phase reads
    # with identical books. Correlation is safe only when there is one distinct
    # physical successful attempt, not merely one archive quote cluster.
    original_groups = {id(row):row['book_evidence_id'] for rows in archive_books.values() for row in rows}
    group_attempts = defaultdict(set)
    for fact in facts:
        if fact['kind'] != 'book' or fact['identifier'] not in universe:
            continue
        try:
            _, _, depth_hash = _book(fact['body']['orderbook_fp']['yes_dollars'],
                fact['body']['orderbook_fp']['no_dollars'])
        except (ValueError, KeyError, TypeError):
            continue
        possible = archive_books.get((fact['identifier'], depth_hash), [])
        exact = [row for row in possible if row['quote_time'] == fact['quote']]
        interval = [row for row in possible if isinstance(row.get('archive_provenance'), dict)
            and type(row['archive_provenance'].get('quote_started_at')) in (int,float)
            and row['archive_provenance']['quote_started_at'] <= fact['started']
            and fact['quote'] <= row['archive_provenance'].get('quote_received_at', -1)]
        for row in exact or interval:
            group_attempts[original_groups[id(row)]].add(fact['token'])
    source_series, source_events, contexts = {}, {}, {}
    by_clock = {}
    merged, new_rows = [], []
    items = [(row.get('available_at') if row['kind'] in SETTLEMENTS else row['observed_at'], 1,
              index, 'original', row) for index, row in enumerate(copied)]
    items.extend((fact['visible'], 0, index, 'capture', fact) for index, fact in enumerate(facts))
    items.sort(key=lambda item: item[:3])

    def context(market, visible, proof):
        if not isinstance(market, dict) or market.get('ticker') not in universe:
            return None
        ticker = market['ticker']
        aligned, spellings = _aligned_market(market, identities[ticker])
        if aligned is None:
            exclusions['partial_or_unpublished_market_identity'] += 1
            return None
        if spellings:
            proof = dict(proof, identity_representation_normalized_fields=spellings)
            counts['market_identity_representation_views_normalized'] += 1
        previous_context = contexts.get(ticker)
        # Retain earlier already-known optional grid/source fields when the
        # newer raw market reply omits them. No existing value is fabricated.
        value = dict(previous_context['market']) if previous_context else {}
        value.update(aligned)
        contexts[ticker] = dict(market=value, visible=visible, proof=proof)
        return aligned

    def gap(fact, reason):
        ticker = fact['identifier']
        value = dict(kind='kalshi_scheduled_book_gap', version=1,
            asset=asset_from_ticker(ticker).symbol, ticker=ticker,
            observed_at=fact['visible'], quote_time=fact['quote'],
            contract_identity=deepcopy(identities[ticker]), gap_reason=reason,
            recovery_provenance=dict(request=deepcopy(fact['proof']),
                evidence='successful_response_not_a_usable_recorded_book'))
        if bundle.get('synthetic') is True:
            value['synthetic'] = True
        new_rows.append(value)
        counts['book_gap_events_recovered'] += 1

    for visible, _, _, source_kind, value in items:
        if source_kind == 'original':
            row = value
            if row['kind'] in OBSERVATIONS:
                recovered = _attach_certificate(row, source_series, source_events, counts, exclusions)
                if recovered is not row:
                    row.update(recovered)
                    if bundle.get('synthetic') is True:
                        row['synthetic'] = True
                context(row.get('market'), visible, dict(source='original_forecast',
                    archive_provenance=deepcopy(row.get('archive_provenance'))))
            merged.append(row)
            continue
        fact = value
        kind, identifier, body = fact['kind'], fact['identifier'], fact['body']
        if kind == 'series':
            document = body.get('series')
            # Even a conflicting newest response supersedes the earlier
            # response as known evidence; do not search backward for a more
            # convenient source certificate.
            source_series[identifier] = dict(value=document, visible=visible, proof=fact['proof'])
            continue
        if kind == 'event':
            document = body.get('event')
            source_events[identifier] = dict(value=document, visible=visible, proof=fact['proof'])
        markets = ([body.get('market')] if kind == 'market' else body.get('markets', []) if kind in ('markets','event') else [])
        if not isinstance(markets, list) or len(markets) > 5000:
            raise PacingRecoveryError('Captured market lists exceed the recovery bound')
        for market in markets:
            if not isinstance(market, dict) or market.get('ticker') not in universe:
                continue
            if kind == 'market' and market.get('ticker') != identifier:
                raise PacingRecoveryError('Captured market endpoint and body disagree')
            market = context(market, visible, fact['proof'])
            if market is None:
                continue
            if market.get('status') == 'finalized':
                ticker = market['ticker']
                try:
                    settled = _timestamp(market.get('settlement_ts'))
                    label = dict(kind='kalshi_scheduled_settlement', version=1,
                        asset=asset_from_ticker(ticker).symbol, ticker=ticker, available_at=visible,
                        settled_at=settled, contract_identity=deepcopy(identities[ticker]),
                        result=market.get('result'), market=deepcopy(market),
                        recovery_provenance=dict(captured_final_market_response=fact['proof']))
                    if bundle.get('synthetic') is True:
                        label['synthetic'] = True
                    validate_records([label])
                    payout = market.get('settlement_value_dollars')
                    if payout is not None and Decimal(str(payout)) != (1 if label['result']=='yes' else 0):
                        raise PacingRecoveryError('A captured final payout is contradictory')
                except (ValueError, KeyError, TypeError, InvalidOperation):
                    exclusions['invalid_captured_final_market'] += 1
                    continue
                prior = existing_outcomes.get(ticker)
                if prior is not None and prior['result'] != label['result']:
                    raise PacingRecoveryError('Captured and archived authoritative outcomes conflict')
                if prior is None or visible < prior['available_at']:
                    existing_outcomes[ticker] = label
                    new_rows.append(label)
                    counts['earlier_official_outcome_receipts_recovered'] += 1
                else:
                    counts['redundant_official_outcome_responses'] += 1
        if kind != 'book':
            continue
        counts['successful_book_response_bodies'] += 1
        ticker = identifier
        if ticker not in universe:
            exclusions['book_outside_original_forecast_universe'] += 1
            continue
        try:
            asset = asset_from_ticker(ticker).symbol
            if asset not in selected or body.get('ticker', ticker) != ticker:
                raise PacingRecoveryError('Book asset or endpoint identity disagrees')
            opened, closed = (_timestamp(identities[ticker][key]) for key in ('open_time','close_time'))
            if not opened <= fact['quote'] <= visible < closed:
                exclusions['book_outside_open_contract'] += 1
                continue
            try:
                yes, no, body_hash = _book(body['orderbook_fp']['yes_dollars'], body['orderbook_fp']['no_dollars'])
            except (ValueError, KeyError, TypeError):
                exclusions['locked_empty_or_malformed_recorded_book'] += 1
                gap(fact, 'locked_empty_or_malformed_recorded_book')
                continue
            clock_key = ticker, fact['quote'], body_hash
            if clock_key in by_clock:
                counts['duplicate_book_body_at_same_receipt'] += 1
                continue
            by_clock[clock_key] = fact['token']
            possible = archive_books.get((ticker, body_hash), [])
            exact = [row for row in possible if row['quote_time'] == fact['quote']]
            interval = [row for row in possible if isinstance(row.get('archive_provenance'), dict)
                and type(row['archive_provenance'].get('quote_started_at')) in (int,float)
                and row['archive_provenance']['quote_started_at'] <= fact['started']
                and fact['quote'] <= row['archive_provenance'].get('quote_received_at', -1)]
            correlated = exact or interval
            groups = {original_groups[id(row)] for row in correlated}
            if len(groups) > 1 or any(len(group_attempts[group]) > 1 for group in groups):
                exclusions['ambiguous_archive_quote_correlation'] += 1
                continue
            if correlated:
                token = original_groups[id(correlated[0])]
                aliases = [row for row in possible if original_groups[id(row)] == token]
                for row in aliases:
                    row.update(book_evidence_id=fact['token'], book_evidence_quote_time=fact['quote'])
                    provenance = dict(row.get('recovery_provenance') or {})
                    provenance['same_captured_book_response'] = deepcopy(fact['proof'])
                    row['recovery_provenance'] = provenance
                    if bundle.get('synthetic') is True:
                        row['synthetic'] = True
                counts['archival_quote_aliases_linked'] += len(aliases)
                if min(row['observed_at'] for row in aliases) <= visible:
                    counts['later_archival_book_mirrors_omitted'] += 1
                    continue
            known = contexts.get(ticker)
            if known is None or known['visible'] > visible:
                exclusions['book_without_causal_market_context'] += 1
                continue
            market = deepcopy(known['market'])
            if market.get('status') != 'active':
                exclusions['book_with_nonactive_recorded_market'] += 1
                gap(fact, 'book_with_nonactive_recorded_market')
                continue
            meta = market.get('_crypto15m') or market.get('_btc15m') or {}
            if not isinstance(meta, dict):
                raise PacingRecoveryError('Recorded book market fee context is malformed')
            series, event = source_series.get(asset_spec(asset).series), source_events.get(market.get('event_ticker'))
            context_received = known['visible']
            source_proofs = None
            if series is not None and event is not None:
                verified, fee_type, multiplier = _source_proof(market, asset, series['value'], event['value'])
                meta = dict(meta)
                meta.update(verified)
                meta.update(fee_type=fee_type, fee_multiplier=multiplier,
                    settlement_source_evidence=dict(series=deepcopy(series['value']), event=deepcopy(event['value'])))
                market['_crypto15m'] = meta
                context_received = max(context_received, series['visible'], event['visible'])
                source_proofs = dict(series=deepcopy(series['proof']), event=deepcopy(event['proof']))
            if meta.get('fee_type') not in ('quadratic','quadratic_with_maker_fees') or meta.get('fee_multiplier') is None:
                exclusions['book_without_recorded_fee_context'] += 1
                continue
            book_row = dict(kind='kalshi_scheduled_book', version=1, asset=asset, ticker=ticker,
                observed_at=visible, quote_time=fact['quote'], contract_identity=deepcopy(identities[ticker]),
                market=market, yes_bids=yes, no_bids=no, fee_type=meta['fee_type'], fee_multiplier=meta['fee_multiplier'],
                book_evidence_id=fact['token'], book_evidence_quote_time=fact['quote'],
                recovery_provenance=dict(request=fact['proof'], book_body_sha256=body_hash,
                    market_context_received_at=context_received, market_context=deepcopy(known['proof']),
                    source_documents=source_proofs))
            if bundle.get('synthetic') is True:
                book_row['synthetic'] = True
            quantum, quantum_locations = _recorded_quantum(market, body)
            if quantum is not None:
                book_row['fee_quantum'] = quantum
                book_row['fee_quantum_source'] = 'recorded_capture_metadata'
                book_row['recovery_provenance']['fee_quantum_locations'] = quantum_locations
            new_rows.append(book_row)
            counts['book_only_events_recovered'] += 1
        except ValueError:
            exclusions['invalid_or_conflicting_book_evidence'] += 1
            opened, closed = (_timestamp(identities[ticker][key]) for key in ('open_time','close_time'))
            if opened <= fact['quote'] <= visible < closed:
                gap(fact, 'invalid_or_conflicting_book_evidence')
    merged.extend(new_rows)
    if len(merged) > max_events:
        raise PacingRecoveryError('Merged recovery records exceed the configured bound')
    merged.sort(key=lambda row: (row.get('available_at') if row['kind'] in SETTLEMENTS else row['observed_at'],
        0 if row['kind'] in BOOKS+GAPS else 1 if row['kind'] in OBSERVATIONS else 2,
        row['asset'], row['ticker']))
    return dict(records=merged, recovery=dict(kind='kalshi_pacing_data_recovery', version=1,
        original_record_count=len(records), merged_record_count=len(merged),
        original_forecast_contracts=len(universe), counts=dict(counts), exclusions=dict(exclusions),
        capture_coverage=deepcopy(bundle['coverage']), capture_verification=bundle['verification'],
        source_run_id=run_id, execution_eligible=False, synthetic=bundle.get('synthetic') is True,
        event_order='true_receipt_then_book_before_forecast_before_settlement_then_asset_ticker',
        global_coordinator_order_verified=False,
        assumptions=['Recovery retains original forecasts, source timestamps, probability revisions and archive outcome receipts.',
            'Only independently recorded earlier final-market receipts may add an earlier official outcome event.',
            'Captured books are separate events; no probability, model time, or trading decision is synthesized.',
            'Same HTTP-response aliases share one canonical quote clock and depth-consumption token.',
            'Same book at a distinct later HTTP receipt is new measured evidence; identical mirrors at one receipt are deduplicated.',
            'Unknown market context, source conflicts and unrecoverable documents remain explicit exclusions.',
            'Only the provided original forecast-contract universe can receive recovered events.']))
