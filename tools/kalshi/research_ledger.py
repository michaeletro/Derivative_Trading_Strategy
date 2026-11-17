"""Bounded offline attribution from recorded authoritative fills and settlements."""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math

ZERO, ONE = Decimal(0), Decimal(1)
_FIFO_ORDER_AMBIGUITY = 'Same-time fills lack a unique authoritative FIFO ordering'
_OPERATIONAL = {'research_dir', 'research_run_id', 'report', 'activity_report',
    'audit_interval_seconds', 'observation_worker', 'worker_asset',
    'observation_session', 'run', 'allow_live', 'update_entry_schedule',
    'update_risk_limits', 'update_bet_budget', 'update_cash_allocation', 'add_assets',
    'stop_file', 'show_sizing_plan'}


def _identity(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(
        32 <= ord(character) < 127 for character in value) else None


def _decimal(value, *, places=6, signed=False, maximum=Decimal('1000000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        if len(str(value)) > 100:
            return None
        result = Decimal(value)
        if not result.is_finite():
            return None
        shape = result.as_tuple()
        if not -30 <= shape.exponent <= 20 or len(shape.digits) > 100:
            return None
        if abs(result) > maximum or not signed and result < ZERO:
            return None
        with localcontext() as context:
            context.prec = 50
            return result if result == result.quantize(Decimal(1).scaleb(-places)) else None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _text(value):
    with localcontext() as context:
        context.prec = 50
        return None if value is None else format(value.normalize(), 'f') if value else '0'


def _stamp(value):
    try:
        if type(value) in (float, int):
            stamp = float(value)
        elif isinstance(value, str) and len(value) <= 100:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if date.tzinfo is None:
                return None
            stamp = date.timestamp()
        else:
            return None
        return stamp if math.isfinite(stamp) and 0 <= stamp <= 253402300799 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _iso(value):
    stamp = _stamp(value)
    return None if stamp is None else datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _group(values):
    known = {value for value in values if value is not None}
    return ('mixed' if len(known) > 1 or known and any(value is None for value in values)
        else next(iter(known)) if known else None)


def _asset(ticker):
    for symbol in ('BTC', 'ETH', 'SOL', 'XRP', 'GOLD'):
        if isinstance(ticker, str) and ticker.startswith('KX' + symbol + '15M-'):
            return symbol
    return None


def _compact_metadata(metadata):
    settings = _dict(metadata.get('settings'))
    configurations, models = {}, {}
    for asset, model in _dict(metadata.get('models')).items():
        if asset not in ('BTC', 'ETH', 'SOL', 'XRP', 'GOLD'):
            continue
        configurations[asset] = _digest(dict(risk=metadata.get('risk'), model=model,
            settings={key:value for key,value in settings.items() if key not in _OPERATIONAL},
            source_fingerprints=metadata.get('source_fingerprints')))
        models[asset] = _identity(_dict(model).get('model_version'))
    return dict(journal_id=_identity(metadata.get('journal_id')),
        account_fingerprint=_identity(metadata.get('account_fingerprint')),
        strategy=_identity(metadata.get('strategy')), configurations=configurations, model_versions=models)


def _basis_issues(fills):
    issues, same_time = [], defaultdict(list)
    if len({row['exchange_index'] for row in fills}) > 1:
        issues.append('Contract fills span conflicting exchange partitions')
    with localcontext() as context:
        context.prec = 50
        for row in fills:
            quantity = Decimal(row['quantity'])
            same_time[row['timestamp']].append((row['outcome'],
                quantity * Decimal(row['actual_price']) + Decimal(row['actual_fee']), quantity))
        for values in same_time.values():
            first = values[0]
            if any(value[0] != first[0] or value[1] * first[2] != first[1] * value[2]
                    for value in values[1:]):
                issues.append(_FIFO_ORDER_AMBIGUITY)
                break
    return issues


def _financial_state_signature(fills, orders, settlement):
    fill_fields = ('fill_id', 'order_id', 'client_order_id', 'ticker', 'timestamp',
        'exchange_index', 'subaccount', 'outcome', 'signed_quantity', 'quantity',
        'actual_price', 'actual_fee', 'intended_limit')
    order_fields = ('client_order_id', 'order_id', 'ticker', 'outcome',
        'journal_status', 'requested_quantity', 'intended_limit', 'actual_fill_quantity',
        'acknowledged_fill_quantity', 'fill_quantity_reconciled')
    normalized_orders = []
    for row in sorted(orders, key=lambda row: row['client_order_id']):
        normalized = {key: row.get(key) for key in order_fields}
        if 'audited_requested_quantity' in row:
            normalized['requested_quantity'] = row['audited_requested_quantity']
        normalized_orders.append(normalized)
    return _digest(dict(
        fills=[{key: row.get(key) for key in fill_fields}
            for row in sorted(fills, key=lambda row: row['fill_id'])],
        orders=normalized_orders,
        settlement=settlement))


def _verified_ambiguity_only(evidence, fills, orders, evidence_time):
    """Validate a typed fill-history proof without making FIFO lots identifiable.

    The source proof attests collection facts unavailable in emitted owned fills
    (notably absence of external/invalid fills). Recompute every represented fact
    from this cumulative audit; a generic legacy ``unknown`` is never sufficient.
    """
    if (not isinstance(evidence, dict) or evidence.get('hard_basis_status') != 'verified'
            or evidence.get('hard_basis_reasons') != []
            or evidence.get('fifo_ordering_status') != 'ambiguous'
            or evidence.get('history_complete') is not True
            or evidence.get('all_order_quantities_reconciled') is not True
            or not fills or not orders or _basis_issues(fills) != [_FIFO_ORDER_AMBIGUITY]):
        return False
    counts = dict(external_fill_count=0, conflicting_fill_count=0, invalid_fill_count=0,
        owned_fill_count=len(fills), canonical_fill_count=len(fills),
        owned_order_count=len(orders), reconciled_order_count=len(orders), partition_count=1)
    if any(type(evidence.get(key)) is not int or evidence[key] != value for key, value in counts.items()):
        return False
    if len({row['fill_id'] for row in fills}) != len(fills):
        return False
    clients = {row['client_order_id']: row for row in orders}
    if len(clients) != len(orders) or any(row['client_order_id'] not in clients for row in fills):
        return False
    venue_ids = [row['order_id'] for row in orders if row['order_id'] is not None]
    if len(venue_ids) != len(set(venue_ids)):
        return False
    by_client = defaultdict(list)
    for fill in fills:
        by_client[fill['client_order_id']].append(fill)
    for order in orders:
        requested = _decimal(order['requested_quantity'], places=2)
        limit = _decimal(order['intended_limit'], maximum=ONE)
        intent = _stamp(order['intent_time'])
        selected = by_client[order['client_order_id']]
        quantity = sum((Decimal(fill['quantity']) for fill in selected), ZERO)
        if (order['journal_status'] not in ('filled', 'canceled', 'rejected')
                or order['order_id'] is None and (quantity != ZERO or order['journal_status'] not in ('canceled', 'rejected'))
                or order['outcome'] not in ('yes', 'no')
                or requested is None or requested <= ZERO or requested < quantity
                or limit is None or not ZERO < limit < ONE
                or intent is None or intent > evidence_time + 1e-6
                or order['fill_quantity_reconciled'] is not True
                or _decimal(order['actual_fill_quantity'], places=2) != quantity
                or _decimal(order['acknowledged_fill_quantity'], places=2) != quantity
                or any(fill['ticker'] != order['ticker'] or fill['outcome'] != order['outcome']
                    or fill['order_id'] != order['order_id'] or _stamp(fill['timestamp']) < intent
                    for fill in selected)):
            return False
    return True


class ContractLedger:
    """Consume ResearchReader events; retain compact rows, never raw snapshots."""
    def __init__(self, *, max_rows=100000):
        if type(max_rows) is not int or not 1 <= max_rows <= 1000000:
            raise ValueError('Ledger row bound is invalid')
        self.max_rows = max_rows
        self._decisions, self._audits, self._fills = {}, {}, {}
        self._conflicts, self._issues = set(), []
        self._settlement_evidence, self._settlement_conflicts = {}, set()
        self._first_complete_states = {}
        self._audit_clock_signatures, self._conflicting_audit_clocks = {}, set()
        self._run_metadata, self._audit_run_journals = {}, {}
        self._scope_runs, self._scope_account_claims = defaultdict(set), defaultdict(set)
        self._run_account_claims, self._run_journal_claims = defaultdict(set), defaultdict(set)
        self._scope_invalid, self._metadata_conflicted_runs = set(), set()
        self._audit_count = self._invalid = self._omitted = 0

    def _room(self, container, key):
        if key in container:
            return True
        if len(container) >= self.max_rows:
            self._omitted += 1
            return False
        return True

    def consume(self, event, *, metadata=None):
        if not isinstance(event, dict) or _identity(event.get('run_id')) is None:
            self._invalid += 1
            return
        run_id = event['run_id']
        if isinstance(metadata, dict):
            self._record_metadata_claims(run_id, metadata)
        if isinstance(metadata, dict) and run_id not in self._run_metadata and self._room(self._run_metadata, run_id):
            self._run_metadata[run_id] = _compact_metadata(metadata)
        payload = _dict(event.get('payload'))
        if event.get('kind') == 'decision':
            self._decision(event, payload)
        elif event.get('kind') == 'activity_audit':
            self._audit(event, payload)

    def _record_metadata_claims(self, run, metadata):
        for field, target in (('account_fingerprint', self._run_account_claims),
                ('journal_id', self._run_journal_claims)):
            value = _identity(metadata.get(field))
            if value is not None and self._room(target, run):
                target[run].add(value)

    def _decision(self, event, payload):
        identifier = _identity(event.get('decision_id')) or _identity(event.get('event_id'))
        key = (event['run_id'], identifier)
        if identifier is None or not self._room(self._decisions, key):
            return
        observation, execution = _dict(payload.get('observation')), _dict(payload.get('execution'))
        forecast = _dict(observation.get('forecast'))
        contract, snapshot = _dict(forecast.get('contract_identity')), _dict(observation.get('market_snapshot'))
        quote = _dict(snapshot.get('market'))
        ticker = _identity(execution.get('ticker')) or _identity(forecast.get('ticker')) or _identity(event.get('ticker'))
        outcome = execution.get('outcome') or forecast.get('median_outcome') or forecast.get('selected_outcome')
        outcome = outcome if outcome in ('yes', 'no') else None
        try:
            probability = forecast.get('probability_' + (outcome or 'yes'))
            if isinstance(probability, bool):
                raise ValueError()
            probability = float(probability)
            probability = probability if math.isfinite(probability) and 0 <= probability <= 1 else None
        except (TypeError, ValueError, OverflowError):
            probability = None
        ask = _decimal(quote.get((outcome or '') + '_ask'), maximum=ONE)
        if ask is None:
            ask = _decimal(_dict(_dict(execution.get('scheduled_entry_decision')).get('executable_asks')).get(outcome), maximum=ONE)
        if ask is not None and not ZERO < ask < ONE:
            ask = None
        record = dict(ledger_decision_id=_digest(key), decision_id=identifier, run_id=event['run_id'],
            source_observation_id=_identity(payload.get('source_observation_id')),
            ticker=ticker, asset=_identity(event.get('asset')) or _identity(observation.get('asset')) or _asset(ticker),
            stage=_identity(payload.get('stage')), status=_identity(execution.get('status')),
            reason=execution.get('reason')[:1000] if isinstance(execution.get('reason'), str) else None,
            decision_time=_iso(payload.get('started_at', event.get('recorded_at'))),
            outcome=outcome, selected_probability=probability,
            client_order_id=_identity(execution.get('client_order_id')), order_id=_identity(execution.get('order_id')),
            requested_quantity=_text(_decimal(execution.get('count'), places=2)),
            intended_limit=_text(_decimal(execution.get('price'), maximum=ONE)),
            decision_quote_price=_text(ask), decision_quote_time=_iso(snapshot.get('fetched_at')),
            model_version=_identity(forecast.get('model_version')),
            forecast_configuration_id=_identity(forecast.get('config_hash')),
            open_time=_iso(contract.get('open_time', quote.get('open_time'))),
            close_time=_iso(contract.get('close_time', quote.get('close_time'))))
        if key in self._decisions and self._decisions[key] != record:
            self._issues.append('Conflicting duplicate recorded decision identities')
            self._invalid += 1
        else:
            self._decisions[key] = record

    def _audit(self, event, payload):
        report = _dict(payload.get('report', payload))
        if (report.get('kind') != 'kalshi_scheduled_activity_report' or report.get('read_only') is not True
                or report.get('execution_eligible') is not False or report.get('reservations_are_actual_costs') is not False):
            return
        journal = _identity(payload.get('journal_id')) or _identity(self._run_metadata.get(event['run_id'], {}).get('journal_id'))
        journal = journal or 'legacy-run:' + event['run_id']
        account = _identity(payload.get('account_fingerprint')) or _identity(report.get('account_fingerprint'))
        scope, stamp = (account, journal), _stamp(event.get('recorded_at'))
        if not self._room(self._audit_run_journals, event['run_id']):
            return
        self._audit_run_journals[event['run_id']] = scope
        if self._room(self._scope_runs, scope):
            self._scope_runs[scope].add(event['run_id'])
        for value in (payload.get('account_fingerprint'), report.get('account_fingerprint')):
            if _identity(value) is not None and self._room(self._scope_account_claims, scope):
                self._scope_account_claims[scope].add(value)
        coverage = _dict(report.get('coverage'))
        generated = _stamp(report.get('generated_at'))
        valid_clock = generated is not None and generated <= stamp + 1e-6 if stamp is not None else False
        evidence_time = min(generated, stamp) if valid_clock else stamp
        orders, events, settlements = (report.get(key) for key in ('orders', 'events', 'settlement_events'))
        if (stamp is None or any(not isinstance(value, list) for value in (orders, events, settlements))
                or max(len(orders), len(events), len(settlements)) > self.max_rows):
            self._invalid += 1
            self._issues.append('An activity audit lacks bounded detailed order/fill/settlement records')
            return
        normalized_orders, fill_keys, bad, source_unknown = {}, set(), set(), set()
        audit_fills, order_counts = defaultdict(list), defaultdict(int)
        contradictory_tied_qualifiers = set()
        invalid_order_rows = invalid_unscoped_evidence = False
        for row in orders:
            row = _dict(row)
            client, ticker = _identity(row.get('client_order_id')), _identity(row.get('ticker'))
            if client is None or ticker is None:
                self._invalid += 1
                invalid_order_rows = True
                self._scope_invalid.add((scope, ticker))
                continue
            order_counts[ticker] += 1
            record = dict(client_order_id=client, order_id=_identity(row.get('order_id')),
                ticker=ticker, asset=_identity(row.get('asset')) or _asset(ticker),
                outcome=row.get('outcome'), intent_time=_iso(row.get('intent_time')),
                journal_status=row.get('journal_status'), requested_quantity=_text(_decimal(row.get('requested_quantity'), places=2)),
                intended_limit=_text(_decimal(row.get('intended_limit'), maximum=ONE)),
                actual_fill_quantity=_text(_decimal(row.get('actual_fill_quantity'), places=2)),
                acknowledged_fill_quantity=_text(_decimal(row.get('acknowledged_fill_quantity'), places=2)),
                fill_quantity_reconciled=row.get('fill_quantity_reconciled') is True,
                exit_reason=_identity(row.get('exit_reason')))
            if client in normalized_orders and normalized_orders[client] != record:
                bad.add(ticker)
            normalized_orders[client] = record
        for raw in events:
            row = _dict(raw)
            fill, ticker = _identity(row.get('fill_id')), _identity(row.get('ticker'))
            quantity, signed = _decimal(row.get('quantity'), places=2), _decimal(row.get('signed_quantity'), places=2, signed=True)
            price, fee = _decimal(row.get('actual_price'), maximum=ONE), _decimal(row.get('actual_fee'), signed=True)
            client, order = _identity(row.get('client_order_id')), _identity(row.get('order_id'))
            time, exchange = _iso(row.get('timestamp')), row.get('exchange_index')
            if (fill is None or ticker is None or quantity is None or quantity <= ZERO or signed is None
                    or abs(signed) != quantity or price is None or not ZERO < price < ONE or fee is None
                    or client is None or order is None or time is None or type(exchange) is not int
                    or not 0 <= exchange <= 1000 or row.get('outcome') != ('yes' if signed > ZERO else 'no')
                    or type(row.get('subaccount', 0)) is not int or row.get('subaccount', 0) != 0):
                self._invalid += 1
                if ticker is not None:
                    bad.add(ticker)
                else:
                    invalid_unscoped_evidence = True
                self._scope_invalid.add((scope, ticker))
                continue
            if evidence_time is None or _stamp(time) > evidence_time + 1e-6:
                bad.add(ticker)
                self._invalid += 1
                self._issues.append('A fill timestamp is after its audit generation or recording time')
            key = (scope, fill)
            if not self._room(self._fills, key):
                bad.add(ticker)
                continue
            record = dict(fill_id=fill, order_id=order, client_order_id=client, ticker=ticker,
                asset=_identity(row.get('asset')) or _asset(ticker), timestamp=time, exchange_index=exchange,
                subaccount=0, outcome=row['outcome'], signed_quantity=_text(signed), quantity=_text(quantity),
                actual_price=_text(price), actual_fee=_text(fee),
                intended_limit=_text(_decimal(row.get('intended_limit'), maximum=ONE)))
            audit_fills[ticker].append(record)
            prior = self._fills.get(key)
            if prior is not None and prior != record:
                self._conflicts.add(key)
                bad.update((ticker, prior['ticker']))
            else:
                self._fills[key] = record
            fill_keys.add(fill)
            if 'fill_history_basis_status' in row and row['fill_history_basis_status'] != 'verified':
                bad.add(ticker)
            if 'fifo_ordering_status' in row and row['fifo_ordering_status'] != 'ambiguous':
                contradictory_tied_qualifiers.add(ticker)
            if row.get('basis_status') == 'unknown':
                source_unknown.add(ticker)
            elif row.get('basis_status') != 'known':
                bad.add(ticker)
        normalized_settlements, unverified_tied_settlements = {}, set()
        for raw in settlements:
            row = _dict(raw)
            ticker, time = _identity(row.get('ticker')), _iso(row.get('timestamp'))
            quantity = _decimal(row.get('owned_net_quantity'), places=2, signed=True)
            payout, fee = _decimal(row.get('attributable_payout')), _decimal(row.get('settlement_fee'), signed=True)
            if (ticker is None or quantity is None or payout is None or fee is None or time is None
                    or row.get('market_result') not in ('yes', 'no')
                    or type(row.get('subaccount', 0)) is not int or row.get('subaccount', 0) != 0
                    or row.get('exchange_index') is not None and (type(row['exchange_index']) is not int
                        or not 0 <= row['exchange_index'] <= 1000)):
                self._invalid += 1
                if ticker is not None:
                    bad.add(ticker)
                else:
                    invalid_unscoped_evidence = True
                self._scope_invalid.add((scope, ticker))
                continue
            record = dict(ticker=ticker, timestamp=time, market_result=row['market_result'],
                owned_net_quantity=_text(quantity), attributable_payout=_text(payout), settlement_fee=_text(fee),
                exchange_index=row.get('exchange_index'), subaccount=row.get('subaccount', 0))
            if evidence_time is None or _stamp(time) > evidence_time + 1e-6:
                bad.add(ticker)
                self._invalid += 1
                self._issues.append('A settlement timestamp is after its audit generation or recording time')
            if ticker in normalized_settlements and normalized_settlements[ticker] != record:
                bad.add(ticker)
            normalized_settlements[ticker] = record
            if (row.get('fill_history_basis_status') != 'verified'
                    or row.get('fifo_ordering_status') != 'ambiguous'
                    or row.get('settlement_quantity_status') != 'verified'):
                unverified_tied_settlements.add(ticker)
            settlement_key = (scope, ticker)
            prior_settlement = self._settlement_evidence.get(settlement_key)
            if prior_settlement is not None and prior_settlement != record:
                self._settlement_conflicts.add(settlement_key)
                bad.add(ticker)
            elif self._room(self._settlement_evidence, settlement_key):
                self._settlement_evidence[settlement_key] = record
        unknown = coverage.get('unknown_basis_tickers', [])
        valid_unknown = isinstance(unknown, list) and all(_identity(value) is not None for value in unknown)
        if not valid_unknown:
            unknown = [row['ticker'] for row in normalized_orders.values()]
            self._invalid += 1
            bad.update(unknown)
        source_unknown.update(ticker for ticker in unknown if _identity(ticker) is not None)
        # Only a new, complete, contract-scoped source proof may distinguish the
        # source's generic unknown marker from hard missing/conflicting basis.
        # Keep independent parser, clock and immutable-identity failures in bad.
        typed = _dict(report.get('basis_evidence'))
        identity_failures = {'invalid_authoritative_fill', 'conflicting_fill_identity', 'fill_journal_mismatch',
            'invalid_journal_order_evidence', 'duplicate_journal_client_id', 'duplicate_journal_order_id'}
        for ticker, proof in typed.items():
            proof_reasons = _dict(proof).get('hard_basis_reasons', [])
            if _identity(ticker) is not None and isinstance(proof_reasons, list) and any(reason in identity_failures
                    for reason in proof_reasons if isinstance(reason, str)):
                self._scope_invalid.add((scope, ticker))
        if not typed and any(type(coverage.get(key)) is int and coverage[key] > 0
                for key in ('invalid_fill_count', 'conflicting_fill_count')):
            self._scope_invalid.add((scope, None))
        valid_scope = (account is not None and account == report.get('account_fingerprint')
            and not journal.startswith('legacy-run:')
            and report.get('account_scope') == 'primary_subaccount_0'
            and report.get('read_only_venue') is True and report.get('state_changes_applied') is False)
        if (type(report.get('basis_evidence_version')) is int and report['basis_evidence_version'] == 1
                and valid_scope and valid_clock and valid_unknown
                and not invalid_order_rows and not invalid_unscoped_evidence
                and coverage.get('history_complete', coverage.get('complete')) is True):
            proof_orders = defaultdict(list)
            for row in normalized_orders.values():
                proof_orders[row['ticker']].append(row)
            for ticker in list(source_unknown):
                contract_orders = proof_orders[ticker]
                if (ticker not in bad and ticker not in contradictory_tied_qualifiers
                        and order_counts[ticker] == len(contract_orders)
                        and ticker not in unverified_tied_settlements
                        and _verified_ambiguity_only(typed.get(ticker), audit_fills[ticker],
                            contract_orders, evidence_time)):
                    source_unknown.remove(ticker)
        bad.update(source_unknown)
        if not valid_clock:
            bad.update(row['ticker'] for row in normalized_orders.values())
            bad.update(row['ticker'] for (selected_scope, _), row in self._fills.items() if selected_scope == scope)
            self._issues.append('An authoritative audit lacks causal generation/recording timestamps')
            self._invalid += 1
        normalized = dict(scope=scope, run_id=event['run_id'], recorded_at=stamp,
            valid_clock=valid_clock,
            collection_complete=all(_dict(_dict(report.get('collection')).get(name)).get('complete') is True
                for name in ('current', 'historical')),
            authoritative_ownership=(account is not None and report.get('account_fingerprint') == account
                and report.get('account_scope') == 'primary_subaccount_0'
                and report.get('read_only_venue') is True and report.get('state_changes_applied') is False),
            history_complete=coverage.get('history_complete', coverage.get('complete')) is True,
            orders=normalized_orders, fill_ids=fill_keys, settlements=normalized_settlements, bad_tickers=bad)
        self._audit_count += 1
        prior = self._audits.get(scope)
        clock_key, signature = (scope, stamp), self._audit_signature(normalized)
        prior_signature = self._audit_clock_signatures.get(clock_key)
        if prior_signature is not None and signature != prior_signature:
            self._conflicting_audit_clocks.add(clock_key)
        if clock_key in self._conflicting_audit_clocks:
            self._issues.append('Conflicting same-time cumulative activity audits')
            normalized['bad_tickers'].update(row['ticker'] for row in normalized_orders.values())
            for key, first_stamp in list(self._first_complete_states.items()):
                if key[0] == scope and first_stamp == stamp:
                    del self._first_complete_states[key]
        elif self._room(self._audit_clock_signatures, clock_key):
            self._audit_clock_signatures[clock_key] = signature
        else:
            normalized['bad_tickers'].update(row['ticker'] for row in normalized_orders.values())
        if valid_clock and normalized['history_complete']:
            self._remember_complete_states(normalized)
        if prior is None or stamp >= prior['recorded_at']:
            retained = sum(len(value['orders']) + len(value['settlements']) + len(value['fill_ids'])
                for key, value in self._audits.items() if key != scope)
            incoming = len(normalized_orders) + len(normalized_settlements) + len(fill_keys)
            if retained + incoming > self.max_rows:
                self._omitted += incoming
            elif self._room(self._audits, scope):
                self._audits[scope] = normalized

    @staticmethod
    def _audit_signature(audit):
        return _digest(dict(history_complete=audit['history_complete'], valid_clock=audit.get('valid_clock'),
            collection_complete=audit.get('collection_complete'),
            authoritative_ownership=audit.get('authoritative_ownership'), orders=audit['orders'],
            fill_ids=sorted(audit['fill_ids']), settlements=audit['settlements'], bad_tickers=sorted(audit['bad_tickers'])))

    def _remember_complete_states(self, audit):
        scope = audit['scope']
        by_ticker, by_client, order_groups = defaultdict(list), defaultdict(list), defaultdict(list)
        bad = set(audit['bad_tickers'])
        for identity in audit['fill_ids']:
            row = self._fills.get((scope, identity))
            if row is None:
                continue
            if (scope, identity) in self._conflicts:
                bad.add(row['ticker'])
            by_ticker[row['ticker']].append(row)
            by_client[row['client_order_id']].append(row)
        for row in audit['orders'].values():
            order_groups[row['ticker']].append(row)
            matched = by_client[row['client_order_id']]
            quantity = sum((Decimal(fill['quantity']) for fill in matched), ZERO)
            if (not row['fill_quantity_reconciled'] or row['journal_status'] in ('sending', 'unknown')
                    or _decimal(row['actual_fill_quantity'], places=2) != quantity
                    or _decimal(row['acknowledged_fill_quantity'], places=2) != quantity
                    or any(fill['order_id'] != row['order_id'] or fill['ticker'] != row['ticker']
                        or fill['outcome'] != row['outcome'] for fill in matched)):
                bad.add(row['ticker'])
        for ticker in set(by_ticker) | set(order_groups) | set(audit['settlements']):
            fills = sorted(by_ticker[ticker], key=lambda row: (_stamp(row['timestamp']), row['fill_id']))
            orders, settlement = order_groups[ticker], audit['settlements'].get(ticker)
            if (ticker in bad or any(issue != _FIFO_ORDER_AMBIGUITY for issue in _basis_issues(fills))
                    or (scope, ticker) in self._settlement_conflicts
                    or any(fill['client_order_id'] not in audit['orders'] for fill in fills)):
                continue
            state, _ = _contract_replay(fills, settlement, 'complete-state-check', True)
            if not state['economics_complete'] or state['issues']:
                continue
            key = (scope, ticker, _financial_state_signature(fills, orders, settlement))
            if self._room(self._first_complete_states, key):
                self._first_complete_states[key] = min(audit['recorded_at'],
                    self._first_complete_states.get(key, audit['recorded_at']))

    def _bind_decisions(self, runs):
        coordinators, metadata_conflicts = {}, set(self._metadata_conflicted_runs)
        for run in runs:
            if not isinstance(run, dict) or _identity(run.get('run_id')) is None:
                continue
            self._record_metadata_claims(run['run_id'], _dict(run.get('metadata')))
            if not self._room(self._run_metadata, run['run_id']):
                continue
            compact = _compact_metadata(_dict(run.get('metadata')))
            if run.get('role') == 'coordinator':
                if run['run_id'] in coordinators and coordinators[run['run_id']] != compact:
                    metadata_conflicts.add(run['run_id'])
                coordinators.setdefault(run['run_id'], compact)
                self._run_metadata[run['run_id']] = coordinators[run['run_id']]
            elif run['run_id'] not in self._run_metadata:
                self._run_metadata[run['run_id']] = compact
        if metadata_conflicts:
            self._metadata_conflicted_runs.update(metadata_conflicts)
            self._issues.append('Conflicting coordinator policy/model/source identities share a run')
        records = []
        for original in self._decisions.values():
            row = dict(original)
            metadata = _dict(self._run_metadata.get(row['run_id']))
            scope = getattr(self, '_decision_scope_bindings', {}).get((row['run_id'], row['ticker']),
                self._audit_run_journals.get(row['run_id']))
            journal = metadata.get('journal_id') or (scope[1] if scope else 'legacy-run:' + row['run_id'])
            account = (scope[0] if scope else None) or metadata.get('account_fingerprint')
            configuration = _dict(metadata.get('configurations')).get(row['asset'])
            row.update(journal_id=journal, account_fingerprint=account,
                scope_id=account or 'journal:' + journal, strategy_id=metadata.get('strategy'),
                configuration_id=configuration or ('forecast:' + row['forecast_configuration_id']
                    if row['forecast_configuration_id'] is not None else None),
                configuration_scope='strategy_policy_model_source' if configuration else
                    'forecast_only' if row['forecast_configuration_id'] is not None else 'unavailable')
            row['model_version'] = row['model_version'] or _dict(metadata.get('model_versions')).get(row['asset'])
            if row['run_id'] in metadata_conflicts:
                row.update(configuration_id='mixed', model_version='mixed', configuration_scope='conflicting_run_metadata')
            records.append(row)
        return sorted(records, key=lambda row: (_stamp(row['decision_time']) or 0, row['decision_id']))

    @staticmethod
    def _covered_legacy_contract(legacy_scope, target_scope, ticker, audits, fill_index,
            order_index, conflicts, settlement_conflicts, invalid):
        old, new = audits[legacy_scope], audits[target_scope]
        if not new.get('valid_clock') or not old.get('valid_clock'):
            return 'noncausal_audit_clock'
        if not new.get('authoritative_ownership') or not new.get('ownership_clock_valid', True):
            return 'unverified_bound_account_scope'
        if new['recorded_at'] < old['recorded_at']:
            return 'bound_audit_older_than_legacy'
        if not new.get('collection_complete'):
            return 'bound_collection_incomplete'
        if (legacy_scope, None) in invalid or (legacy_scope, ticker) in invalid:
            return 'legacy_invalid_identity_evidence'
        if (target_scope, None) in invalid or (target_scope, ticker) in invalid:
            return 'bound_invalid_identity_evidence'
        selected_old = fill_index.get((legacy_scope, ticker), {})
        selected_new = {identity: row for identity, row in fill_index.get((target_scope, ticker), {}).items()
            if identity in new['fill_ids']}
        if not selected_old:
            return 'no_authoritative_fill_anchor'
        if any((legacy_scope, identity) in conflicts or (target_scope, identity) in conflicts
                for identity in selected_old):
            return 'conflicting_fill_identity'
        if not set(selected_old) <= old['fill_ids']:
            return 'legacy_cumulative_fill_coverage_incomplete'
        if any(selected_new.get(identity) != row for identity, row in selected_old.items()):
            return 'bound_fill_identity_or_economics_not_covered'
        old_orders = order_index.get((legacy_scope, ticker), {})
        new_orders = new['orders']
        old_quantities, new_quantities = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for row in selected_old.values():
            old_quantities[row['client_order_id']] += Decimal(row['quantity'])
            intent = old_orders.get(row['client_order_id'])
            if (intent is None or intent['order_id'] != row['order_id']
                    or intent['outcome'] != row['outcome'] or intent['ticker'] != row['ticker']):
                return 'legacy_unmatched_fill_identity'
        for row in selected_new.values():
            new_quantities[row['client_order_id']] += Decimal(row['quantity'])
        for client, row in old_orders.items():
            current = new_orders.get(client)
            immutable = ('client_order_id', 'ticker', 'outcome', 'intent_time', 'requested_quantity', 'intended_limit')
            if current is None or any(row.get(key) != current.get(key) for key in immutable):
                return 'bound_order_intent_not_covered'
            old_qty, new_qty = old_quantities[client], new_quantities[client]
            requested = _decimal(row['requested_quantity'], places=2)
            actual, acknowledged, new_actual, new_ack = (_decimal(item, places=2) for item in
                (row['actual_fill_quantity'], row['acknowledged_fill_quantity'],
                 current['actual_fill_quantity'], current['acknowledged_fill_quantity']))
            pending = row['journal_status'] in ('sending', 'unknown')
            if (None in (requested, actual, acknowledged, new_actual, new_ack)
                    or requested <= ZERO or actual != old_qty or new_actual != new_qty
                    or not actual <= new_actual <= requested or not acknowledged <= new_ack <= requested
                    or not pending and (acknowledged != actual or current['journal_status'] != row['journal_status'])):
                return 'unexplained_order_quantity_or_status_change'
            if row['order_id'] != current['order_id']:
                if not (pending and row['order_id'] is None and actual == acknowledged == ZERO
                        and current['order_id'] is not None):
                    return 'bound_venue_order_identity_not_covered'
            if row['order_id'] is None and (actual != ZERO or row['journal_status'] not in ('sending', 'unknown', 'canceled', 'rejected')):
                return 'legacy_unverified_venue_order_identity'
        if any(row['client_order_id'] not in old_orders for row in selected_old.values()):
            return 'legacy_unmatched_fill_identity'
        if (legacy_scope, ticker) in settlement_conflicts or (target_scope, ticker) in settlement_conflicts:
            return 'conflicting_settlement_identity'
        if ticker in old['settlements'] and new['settlements'].get(ticker) != old['settlements'][ticker]:
            return 'bound_settlement_not_covered'
        return None

    def _canonical_scope_view(self):
        """Derive ownership aliases without changing captured financial evidence.

        Metadata is available only after all shards have been read. A later
        authenticated cumulative audit can supersede exactly covered legacy
        contract fragments; its own current bad basis and proof clock survive.
        """
        aliases, identity_issues = {}, defaultdict(set)
        for scope in self._audits:
            account, journal = scope
            runs = self._scope_runs.get(scope, {self._audits[scope]['run_id']})
            claims = set(self._scope_account_claims.get(scope, ()))
            all_explicit = bool(runs)
            for run in runs:
                declared = self._run_account_claims.get(run, set())
                journals = self._run_journal_claims.get(run, set())
                claims.update(declared)
                all_explicit = all_explicit and len(declared) == 1
                if (len(declared) > 1
                        or any(value != journal for value in journals)):
                    identity_issues[scope].add('Captured run metadata has conflicting account or journal ownership')
            if len(claims) > 1 or account is not None and claims and claims != {account}:
                identity_issues[scope].add('Audit account identity conflicts with captured account ownership')
            if account is None and all_explicit and len(claims) == 1 and not identity_issues[scope]:
                aliases[scope] = (next(iter(claims)), journal)
        mapped = lambda scope: aliases.get(scope, scope)
        view = object.__new__(type(self))
        view.__dict__ = dict(self.__dict__)
        view._issues, view._audits, view._fills = list(self._issues), {}, {}
        view._conflicts = {(mapped(scope), identity) for scope, identity in self._conflicts}
        view._settlement_conflicts = {(mapped(scope), ticker) for scope, ticker in self._settlement_conflicts}
        settlement_evidence = {}
        for (scope, ticker), row in self._settlement_evidence.items():
            key = mapped(scope), ticker
            if key in settlement_evidence and settlement_evidence[key] != row:
                view._settlement_conflicts.add(key)
            else:
                settlement_evidence[key] = row
        view._scope_invalid = {(mapped(scope), ticker) for scope, ticker in self._scope_invalid}
        view._first_complete_states, view._audit_clock_signatures = {}, {}
        view._conflicting_audit_clocks = {(mapped(scope), stamp) for scope, stamp in self._conflicting_audit_clocks}
        view._audit_run_journals = {run: mapped(scope) for run, scope in self._audit_run_journals.items()}
        view._scope_identity_issues, view._ownership_proof_floors = defaultdict(set), {}
        view._decision_scope_bindings = {}
        stats = dict(explicit_metadata_scope_count=len(aliases), covered_legacy_contract_count=0,
            retained_legacy_contract_count=0, rejection_reason_counts={})
        rejected = defaultdict(int)
        for scope, values in identity_issues.items():
            view._scope_identity_issues[mapped(scope)].update(values)
        for scope, original in self._audits.items():
            target = mapped(scope)
            audit = dict(original, scope=target, orders=dict(original['orders']),
                fill_ids=set(original['fill_ids']), settlements=dict(original['settlements']),
                bad_tickers=set(original['bad_tickers']))
            prior = view._audits.get(target)
            if prior is None or audit['recorded_at'] >= prior['recorded_at']:
                view._audits[target] = audit
        for (scope, identity), row in self._fills.items():
            key = mapped(scope), identity
            if key in view._fills and view._fills[key] != row:
                view._conflicts.add(key)
            else:
                view._fills[key] = row
        for (scope, ticker, signature), stamp in self._first_complete_states.items():
            key = mapped(scope), ticker, signature
            view._first_complete_states[key] = min(stamp, view._first_complete_states.get(key, stamp))
        for (scope, stamp), signature in self._audit_clock_signatures.items():
            key = mapped(scope), stamp
            if key in view._audit_clock_signatures and view._audit_clock_signatures[key] != signature:
                view._conflicting_audit_clocks.add(key)
            else:
                view._audit_clock_signatures[key] = signature
        for key, stamp in list(view._first_complete_states.items()):
            if (key[0], stamp) in view._conflicting_audit_clocks:
                del view._first_complete_states[key]
        for scope, audit in view._audits.items():
            audit['ownership_clock_valid'] = (scope, audit['recorded_at']) not in view._conflicting_audit_clocks
            if view._scope_identity_issues[scope] or (scope, audit['recorded_at']) in view._conflicting_audit_clocks:
                audit['bad_tickers'].update(row['ticker'] for row in audit['orders'].values())
        candidates, owners = defaultdict(set), defaultdict(set)
        fill_index, order_index, scope_tickers = defaultdict(dict), defaultdict(dict), defaultdict(set)
        for scope in view._audits:
            if scope[0] is not None:
                candidates[scope[1]].add(scope)
            for key, row in view._audits[scope]['orders'].items():
                order_index[scope, row['ticker']][key] = row
                scope_tickers[scope].add(row['ticker'])
            scope_tickers[scope].update(view._audits[scope]['settlements'])
        for (scope, identity), row in view._fills.items():
            fill_index[scope, row['ticker']][identity] = row
            scope_tickers[scope].add(row['ticker'])
            if scope[0] is not None:
                owners[row['exchange_index'], identity].add(scope)
        for scope in list(view._audits):
            if scope[0] is not None:
                continue
            audit = view._audits[scope]
            tickers = scope_tickers[scope]
            for ticker in sorted(tickers):
                targets = candidates[scope[1]]
                target = next(iter(targets)) if len(targets) == 1 else None
                reason = ('no_unique_authenticated_journal_owner' if target is None else
                    'conflicting_account_ownership' if view._scope_identity_issues[scope]
                        or view._scope_identity_issues[target] else None)
                if reason is None:
                    reason = self._covered_legacy_contract(scope, target, ticker, view._audits,
                        fill_index, order_index, view._conflicts, view._settlement_conflicts, view._scope_invalid)
                selected = list(fill_index.get((scope, ticker), {}).items())
                if reason is None and any(owners[row['exchange_index'], identity] - {target} for identity, row in selected):
                    reason = 'multiple_authenticated_fill_owners'
                if reason is not None:
                    rejected[reason] += 1
                    stats['retained_legacy_contract_count'] += 1
                    continue
                # The bound cumulative state is authoritative, including any
                # unresolved basis. Do not blend old costs, statuses or proof.
                bound = view._audits[target]
                for identity, _ in selected:
                    del view._fills[scope, identity]
                    audit['fill_ids'].discard(identity)
                for key in order_index.get((scope, ticker), {}):
                    audit['orders'].pop(key, None)
                audit['settlements'].pop(ticker, None)
                audit['bad_tickers'].discard(ticker)
                view._ownership_proof_floors[target, ticker] = bound['recorded_at']
                for run in self._scope_runs.get(scope, {audit['run_id']}):
                    view._decision_scope_bindings[run, ticker] = target
                stats['covered_legacy_contract_count'] += 1
            if not audit['orders'] and not audit['fill_ids'] and not audit['settlements']:
                del view._audits[scope]
        stats['rejection_reason_counts'] = dict(sorted(rejected.items()))
        view._scope_canonicalization = stats
        return view

    def summary(self, *, runs=(), capture_coverage=None):
        decisions = self._bind_decisions(runs)
        self = self._canonical_scope_view()
        decisions = self._bind_decisions(())
        by_client = defaultdict(list)
        for decision in decisions:
            if decision['client_order_id'] is not None:
                by_client[(decision['journal_id'], decision['client_order_id'])].append(decision)
        fills, orders, settlements, contracts, cycles = [], [], [], [], []
        all_issues = list(self._issues)
        fill_owners = defaultdict(set)
        for (scope, fill), row in self._fills.items():
            fill_owners[(row['exchange_index'], fill)].add(scope)
        duplicate_scopes = {identity for identity, values in fill_owners.items() if len(values) > 1}
        if duplicate_scopes:
            all_issues.append('Venue fill identities occur in multiple journal/authentication scopes; overlapping economics remain unknown')
        scoped_fills = defaultdict(list)
        for (scope, identity), row in self._fills.items():
            scoped_fills[scope].append((identity, row))
        for scope, audit in sorted(self._audits.items(), key=lambda item: str(item[0])):
            account, journal = scope
            common = dict(account_fingerprint=account, journal_id=journal, scope_id=account or 'journal:' + journal)
            bad, grouped, order_rows = set(audit['bad_tickers']), defaultdict(list), {}
            overlap_tickers = set()
            bad.update(ticker for selected_scope, ticker in self._settlement_conflicts if selected_scope == scope)
            for client, original in audit['orders'].items():
                linked = by_client.get((journal, client), [])
                origin = next((row for row in linked if row['status'] in ('filled', 'canceled', 'unknown', 'sending')), None)
                origin = origin or (linked[0] if linked else {})
                row = dict(original, ledger_order_id=_digest([scope, client]),
                    audited_requested_quantity=original['requested_quantity'],
                    run_ids=sorted({item['run_id'] for item in linked}),
                    decision_ids=[item['decision_id'] for item in linked],
                    originating_decision_id=origin.get('decision_id'), strategy_id=origin.get('strategy_id'),
                    originating_ledger_decision_id=origin.get('ledger_decision_id'),
                    configuration_id=origin.get('configuration_id'), model_version=origin.get('model_version'),
                    forecast_configuration_id=origin.get('forecast_configuration_id'),
                    decision_quote_price=origin.get('decision_quote_price'), decision_quote_time=origin.get('decision_quote_time'),
                    open_time=origin.get('open_time'), close_time=origin.get('close_time'), **common)
                if row['requested_quantity'] is None:
                    row['requested_quantity'] = origin.get('requested_quantity')
                row.update(status=row['journal_status'], actual_average_price=None, actual_fees=None)
                order_rows[client] = row
            for identity, original in scoped_fills[scope]:
                row = dict(original)
                order = order_rows.get(row['client_order_id'])
                if (row['exchange_index'], identity) in duplicate_scopes:
                    overlap_tickers.add(row['ticker'])
                if ((scope, identity) in self._conflicts or (row['exchange_index'], identity) in duplicate_scopes
                        or identity not in audit['fill_ids']):
                    bad.add(row['ticker'])
                if (order is None or order['order_id'] != row['order_id'] or order['ticker'] != row['ticker']
                        or order['outcome'] != row['outcome']):
                    bad.add(row['ticker'])
                order = order or {}
                row.update(ledger_fill_id=_digest([scope, identity]), ledger_order_id=order.get('ledger_order_id'),
                    run_ids=order.get('run_ids', []), decision_ids=order.get('decision_ids', []),
                    strategy_id=order.get('strategy_id'), configuration_id=order.get('configuration_id'),
                    model_version=order.get('model_version'), forecast_configuration_id=order.get('forecast_configuration_id'),
                    decision_quote_price=order.get('decision_quote_price'), decision_quote_time=order.get('decision_quote_time'),
                    execution_cost_vs_limit=None, execution_cost_vs_decision_quote=None, **common)
                with localcontext() as context:
                    context.prec = 50
                    limit = _decimal(row.get('intended_limit'), maximum=ONE)
                    if limit is not None:
                        row['execution_cost_vs_limit'] = _text(Decimal(row['quantity']) * (Decimal(row['actual_price']) - limit))
                    quote, quote_time = _decimal(row.get('decision_quote_price'), maximum=ONE), _stamp(row.get('decision_quote_time'))
                    if quote is not None and quote_time is not None and quote_time <= _stamp(row['timestamp']):
                        row['execution_cost_vs_decision_quote'] = _text(Decimal(row['quantity']) * (Decimal(row['actual_price']) - quote))
                grouped[row['ticker']].append(row)
            orders_by_ticker = defaultdict(list)
            fills_by_client = defaultdict(list)
            for selected in grouped.values():
                for fill in selected:
                    fills_by_client[fill['client_order_id']].append(fill)
            for row in order_rows.values():
                selected = fills_by_client[row['client_order_id']]
                quantity = sum((Decimal(fill['quantity']) for fill in selected), ZERO)
                if (not row['fill_quantity_reconciled'] or row['journal_status'] in ('sending', 'unknown')
                        or _decimal(row['actual_fill_quantity'], places=2) != quantity
                        or _decimal(row['acknowledged_fill_quantity'], places=2) != quantity):
                    bad.add(row['ticker'])
                row['actual_fill_quantity'] = _text(quantity)
                row['actual_fees'] = _text(sum((Decimal(fill['actual_fee']) for fill in selected), ZERO))
                with localcontext() as context:
                    context.prec = 50
                    row['actual_average_price'] = (_text(sum((Decimal(fill['quantity']) * Decimal(fill['actual_price'])
                        for fill in selected), ZERO) / quantity) if quantity else None)
                orders_by_ticker[row['ticker']].append(row)
            tickers = set(grouped) | set(orders_by_ticker) | set(audit['settlements'])
            for ticker in sorted(tickers):
                selected_fills = sorted(grouped[ticker], key=lambda row: (_stamp(row['timestamp']), row['fill_id']))
                selected_orders = orders_by_ticker[ticker]
                exchanges = {row['exchange_index'] for row in selected_fills}
                reasons = []
                if ticker in bad:
                    reasons.append('Incomplete, conflicting or unreconciled fill basis')
                if ticker in overlap_tickers:
                    reasons.append('Authoritative venue fill identity overlaps multiple journal/authentication scopes')
                reasons.extend(sorted(self._scope_identity_issues.get(scope, ())))
                if not audit['history_complete']:
                    reasons.append('Authoritative current/historical fill collection is incomplete')
                reasons.extend(issue for issue in _basis_issues(selected_fills) if issue != _FIFO_ORDER_AMBIGUITY)
                contract_id, fill_complete = _digest([scope, ticker, sorted(exchanges)]), not reasons
                for row in selected_fills:
                    row.update(basis_status='known' if fill_complete else 'unknown', ledger_contract_id=contract_id)
                settlement = audit['settlements'].get(ticker)
                replay, selected_cycles = _contract_replay(selected_fills, settlement, contract_id, fill_complete)
                reasons.extend(replay.pop('issues'))
                configuration = _group([row.get('configuration_id') for row in selected_orders])
                model, strategy = (_group([row.get(key) for row in selected_orders]) for key in ('model_version', 'strategy_id'))
                opened = {row['open_time'] for row in selected_orders if row['open_time'] is not None}
                closed = {row['close_time'] for row in selected_orders if row['close_time'] is not None}
                if len(opened) > 1 or len(closed) > 1:
                    reasons.append('Recorded contract time identities conflict')
                first_complete = None
                if replay['economics_complete'] and not reasons:
                    state = _financial_state_signature(selected_fills, selected_orders, settlement)
                    first_complete = self._first_complete_states.get((scope, ticker, state), audit['recorded_at'])
                    first_complete = max(first_complete, self._ownership_proof_floors.get((scope, ticker), first_complete))
                row = dict(ledger_contract_id=contract_id, ticker=ticker,
                    asset=selected_orders[0]['asset'] if selected_orders else _asset(ticker),
                    exchange_index=next(iter(exchanges)) if len(exchanges) == 1 else None,
                    run_ids=sorted({value for order in selected_orders for value in order['run_ids']}),
                    strategy_id=strategy, configuration_id=configuration, model_version=model,
                    attribution_status='mixed' if 'mixed' in (configuration, model, strategy) else
                        'known' if configuration is not None else 'unavailable',
                    open_time=next(iter(opened)) if len(opened) == 1 else None,
                    close_time=next(iter(closed)) if len(closed) == 1 else None,
                    first_fill_time=selected_fills[0]['timestamp'] if selected_fills else None,
                    last_fill_time=selected_fills[-1]['timestamp'] if selected_fills else None,
                    latest_evidence_at=_iso(audit['recorded_at']), economics_observed_at=_iso(first_complete),
                    ownership_binding_observed_at=_iso(self._ownership_proof_floors.get((scope, ticker))),
                    actual_fill_quantity=_text(sum((Decimal(fill['quantity']) for fill in selected_fills), ZERO)),
                    actual_fees=(_text(sum((Decimal(fill['actual_fee']) for fill in selected_fills), ZERO))
                        if fill_complete else None),
                    known_actual_fees=_text(sum((Decimal(fill['actual_fee']) for fill in selected_fills), ZERO)),
                    actual_fees_complete=fill_complete,
                    fill_count=len(selected_fills), order_count=len(selected_orders), issues=list(dict.fromkeys(reasons)),
                    **common, **replay)
                if reasons:
                    row.update(economics_complete=False, accounting_status='unknown', realized_pnl=None)
                if settlement is not None:
                    settlements.append(dict(settlement, ledger_settlement_id=_digest([scope, ticker, settlement['timestamp']]),
                        ledger_contract_id=contract_id, economics_complete=row['economics_complete'], **common))
                for cycle in selected_cycles:
                    cycle.update(ticker=ticker, asset=row['asset'], run_ids=row['run_ids'], strategy_id=strategy,
                        configuration_id=configuration, model_version=model, open_time=row['open_time'],
                        close_time=row['close_time'], **common)
                for order in selected_orders:
                    order.update(economics_complete=fill_complete, ledger_contract_id=contract_id)
                    order['known_actual_fees'] = order['actual_fees']
                    order['actual_fees_complete'] = fill_complete
                    if not fill_complete:
                        order['actual_fees'] = None
                fills.extend(selected_fills)
                orders.extend(selected_orders)
                contracts.append(row)
                cycles.extend(selected_cycles)
        if self._invalid:
            all_issues.append('Some ledger inputs were invalid or lacked detailed authoritative evidence')
        if self._omitted:
            all_issues.append('The ledger entity bound was reached; omitted rows prevent full coverage')
        if not self._audits:
            all_issues.append('No detailed authoritative activity audit was recorded')
        coverage = _dict(capture_coverage)
        return dict(kind='kalshi_contract_research_ledger', version=1, read_only=True, execution_eligible=False,
            decisions=decisions, orders=orders, fills=fills, settlements=settlements, contracts=contracts, cycles=cycles,
            coverage=dict(complete=not all_issues and not self._invalid and not self._omitted
                and bool(self._audits) and all(row['economics_complete'] for row in contracts),
                contract_economics_complete=not all_issues and not self._invalid and not self._omitted
                    and bool(self._audits) and all(row['economics_complete'] for row in contracts),
                coverage_scope='Final contract economics; FIFO-cycle attribution is reported separately',
                cycle_attribution_complete=bool(self._audits) and all(
                    row.get('cycle_attribution_status') == 'known' for row in contracts),
                order_invariant_recovered_contract_count=sum(
                    row.get('economics_method') == 'order_invariant_cash_flows'
                    and row['economics_complete'] for row in contracts),
                capture_complete=coverage.get('complete'), detailed_audit_count=self._audit_count,
                selected_cumulative_scope_count=len(self._audits), invalid_input_count=self._invalid,
                scope_canonicalization=self._scope_canonicalization,
                omitted_row_count=self._omitted, entity_limit=self.max_rows, issues=list(dict.fromkeys(all_issues))),
            combined_realized_pnl=None,
            accounting_convention='FIFO complementary-outcome netting when ordered; complete flat/settled contract cash flows are order-invariant. Proportional FIFO fees use 50-digit Decimal arithmetic and exact final-lot residuals',
            account_identity_kind='Configured authentication-key fingerprint; absent legacy values remain journal-scoped',
            limitations=['This derived ledger cannot authorize, reconcile or submit an order.',
                'Account fingerprints identify authentication scopes, not independently resolved exchange accounts.',
                'Cumulative audits across runs are deduplicated; cross-scope totals are not summed.',
                'Unknown, conflicting, missing or unmatched fill basis is not a zero-cost position.',
                'Reservations and acknowledgements never supply paid fill prices or fees.',
                'Timestamp-tied fills can establish final flat or verified-settled contract P&L without identifying FIFO lots or position cycles.',
                'Open inventory has no final realized outcome; skipped decisions have no invented counterfactual fills.'])


def _contract_replay(fills, settlement, contract_id, complete):
    """Separate final contract economics from an unidentifiable FIFO path."""
    basis_issues = _basis_issues(fills)
    hard_issues = [issue for issue in basis_issues if issue != _FIFO_ORDER_AMBIGUITY]
    complete = complete and not hard_issues
    ambiguous = _FIFO_ORDER_AMBIGUITY in basis_issues
    if complete and ambiguous:
        return _order_invariant_final(fills, settlement)
    result, cycles = _replay(fills, settlement, contract_id, complete)
    result['issues'].extend(hard_issues)
    result.update(economics_method='fifo' if complete else None,
        cycle_attribution_status='known' if complete and not result['issues'] else 'unavailable',
        fifo_basis_status='known' if complete and not result['issues'] else 'unknown',
        cycle_attribution_issues=[_FIFO_ORDER_AMBIGUITY] if ambiguous else [])
    return result, cycles


def _order_invariant_final(fills, settlement):
    """Final cash-flow identity for a complete history starting flat.

    Each opposing acquisition matches one existing contract and releases one
    dollar. The total matched quantity is min(total YES, total NO), independent
    of their intratimestamp order. Open inventory still needs FIFO basis for
    interim P&L; only flat or independently settled inventory is recovered here.
    """
    base = dict(realized_pnl=None, known_realized_pnl=None, open_quantity=None,
        open_basis=None, net_cash_flow=None, settlement_payout=None, settlement_fee=None,
        accounting_status='unknown', economics_complete=False, issues=[],
        economics_method='order_invariant_cash_flows', cycle_attribution_status='ambiguous',
        fifo_basis_status='unknown', cycle_attribution_issues=[_FIFO_ORDER_AMBIGUITY])
    with localcontext() as context:
        context.prec = 50
        yes = sum((Decimal(row['quantity']) for row in fills if row['outcome'] == 'yes'), ZERO)
        no = sum((Decimal(row['quantity']) for row in fills if row['outcome'] == 'no'), ZERO)
        position = yes - no
        premium = sum((Decimal(row['quantity']) * Decimal(row['actual_price']) for row in fills), ZERO)
        fees = sum((Decimal(row['actual_fee']) for row in fills), ZERO)
        matched = min(yes, no)
        cash = matched - premium - fees
        payout = charge = ZERO
        if settlement is None and position != ZERO:
            base['issues'].append('Ambiguous FIFO ordering requires a flat or verified-settled contract for final accounting')
            return base, []
        if settlement is not None:
            payout, charge = Decimal(settlement['attributable_payout']), Decimal(settlement['settlement_fee'])
            exchanges = {row['exchange_index'] for row in fills}
            last_fill = max((_stamp(row['timestamp']) for row in fills), default=None)
            expected = abs(position) if (position > ZERO) == (settlement['market_result'] == 'yes') else ZERO
            if (Decimal(settlement['owned_net_quantity']) != position
                    or last_fill is not None and _stamp(settlement['timestamp']) < last_fill
                    or settlement.get('subaccount', 0) != 0
                    or settlement.get('exchange_index') is not None and exchanges != {settlement['exchange_index']}
                    or payout != expected):
                base['issues'].append('Settlement does not reconcile to exact inventory, timing, partition and binary payout')
                return base, []
            cash += payout - charge
        base.update(realized_pnl=_text(cash), known_realized_pnl=_text(cash),
            net_cash_flow=_text(cash), open_quantity='0', open_basis='0',
            settlement_payout=_text(payout) if settlement is not None else None,
            settlement_fee=_text(charge) if settlement is not None else None,
            accounting_status='settled' if settlement is not None else 'closed', economics_complete=True,
            cash_flow_components=dict(yes_acquired_quantity=_text(yes), no_acquired_quantity=_text(no),
                matched_complementary_quantity=_text(matched), actual_acquisition_premium=_text(premium),
                signed_fill_fees=_text(fees), attributable_payout=_text(payout), settlement_fee=_text(charge)))
    # The whole-contract result contains no ordering evidence for FIFO cycles.
    return base, []


def _replay(fills, settlement, contract_id, complete):
    base = dict(realized_pnl=None, known_realized_pnl=None, open_quantity=None, open_basis=None,
        net_cash_flow=None, settlement_payout=None, settlement_fee=None,
        accounting_status='unknown', economics_complete=False, issues=[])
    if not complete:
        return base, []
    lots, cycles, current = deque(), [], None
    realized = net_cash = ZERO
    last_time = None
    with localcontext() as context:
        context.prec = 50
        for fill in fills:
            quantity, price, fee = (Decimal(fill[key]) for key in ('quantity', 'actual_price', 'actual_fee'))
            sign = 1 if fill['outcome'] == 'yes' else -1
            remaining, closed, basis = quantity, ZERO, ZERO
            while remaining and lots and lots[0]['sign'] != sign:
                lot = lots[0]
                match = min(remaining, lot['quantity'])
                allocated = lot['fee'] if match == lot['quantity'] else lot['fee'] * match / lot['quantity']
                basis += match * lot['price'] + allocated
                lot['fee'] -= allocated
                lot['quantity'] -= match
                remaining -= match
                closed += match
                if lot['quantity'] == ZERO:
                    lots.popleft()
            closing_fee = fee if closed == quantity else fee * closed / quantity
            opening_fee = fee - closing_fee
            gain = closed * (ONE - price) - basis - closing_fee
            realized += gain
            net_cash += closed - quantity * price - fee
            if current is not None and closed:
                current['_fees'] += closing_fee
                current['_pnl'] += gain
                current['_quantity'] += closed
                if not lots:
                    current.update(closed_at=fill['timestamp'], complete=True, closed_reason='opposite_fill')
                    cycles.append(current)
                    current = None
            if remaining:
                if current is None:
                    current = dict(cycle_id=_digest([contract_id, len(cycles), fill['fill_id']]),
                        ledger_contract_id=contract_id, opened_at=fill['timestamp'], closed_at=None,
                        opening_outcome=fill['outcome'], complete=False, closed_reason=None,
                        _fees=ZERO, _pnl=ZERO, _quantity=ZERO)
                current['_fees'] += opening_fee
                current['_quantity'] += remaining
                lots.append(dict(sign=sign, quantity=remaining, price=price, fee=opening_fee))
            last_time = fill['timestamp']
        position = sum((lot['quantity'] * lot['sign'] for lot in lots), ZERO)
        basis = sum((lot['quantity'] * lot['price'] + lot['fee'] for lot in lots), ZERO)
        payout = settlement_fee = ZERO
        settled = False
        if settlement is not None:
            desired = Decimal(settlement['owned_net_quantity'])
            payout, settlement_fee = Decimal(settlement['attributable_payout']), Decimal(settlement['settlement_fee'])
            valid = (desired == position and (last_time is None or _stamp(settlement['timestamp']) >= _stamp(last_time))
                and settlement.get('subaccount', 0) == 0)
            exchanges = {fill['exchange_index'] for fill in fills}
            if settlement.get('exchange_index') is not None and exchanges != {settlement['exchange_index']}:
                valid = False
            expected = abs(position) if (position > ZERO) == (settlement['market_result'] == 'yes') else ZERO
            if payout != expected:
                valid = False
            if not valid:
                base['issues'].append('Settlement does not reconcile to exact inventory, timing, partition and binary payout')
                return base, []
            gain = payout - basis - settlement_fee
            realized += gain
            net_cash += payout - settlement_fee
            if current is not None:
                current['_pnl'] += gain
                current['_fees'] += settlement_fee
                current.update(closed_at=settlement['timestamp'], complete=True, closed_reason='official_settlement')
                cycles.append(current)
                current = None
            position = basis = ZERO
            settled = True
        if current is not None:
            cycles.append(current)
        for row in cycles:
            row['actual_fees'] = _text(row.pop('_fees'))
            row['filled_quantity'] = _text(row.pop('_quantity'))
            row['known_realized_pnl'] = _text(row.pop('_pnl'))
            row['realized_pnl'] = row['known_realized_pnl'] if row['complete'] else None
        closed = position == ZERO
        base.update(realized_pnl=_text(realized), known_realized_pnl=_text(realized),
            open_quantity=_text(position), open_basis=_text(basis), net_cash_flow=_text(net_cash),
            settlement_payout=_text(payout) if settlement is not None else None,
            settlement_fee=_text(settlement_fee) if settlement is not None else None,
            accounting_status='settled' if settled else 'closed' if closed else 'open', economics_complete=closed)
        return base, cycles
