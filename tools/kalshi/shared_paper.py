"""One authoritative SQLite ledger for guarded, competing PAPER strategies.

No client or exchange writer is owned here. Strategy checkpoints, shared
admission, reservations and durable intent evidence commit in one transaction.
Separate isolated paper journals are deliberately not adopted or reset.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from itertools import product
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import types
import uuid

from .neutral_paper import NeutralPaperConfig, NeutralPaperEngine
from .neutral_model import normalize_neutral_snapshot
from .neutral_paper_journal import _json, _decode, _validate
from .neutral_sizing import NeutralSizingConfig, plan_neutral_spread
from .portfolio_admission import assess_admission
from .value_sizing import ValueSizingConfig, worst_value_buy_unit_cost


ZERO = Decimal(0)
KIND = 'kalshi_shared_paper_coordinator_v1'
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,80}\Z')
_ACTIVE = ('prepared', 'unknown')


class SharedPaperError(ValueError):
    """Fixed public paper-coordination error."""


def _amount(value, *, positive=False):
    try:
        if isinstance(value, bool) or len(str(value)) > 80:
            raise ValueError()
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or result > 1000000
                or positive and not result or not -18 <= result.as_tuple().exponent <= 18):
            raise ValueError()
        return result
    except Exception:
        raise SharedPaperError('Shared paper limits require bounded nonnegative decimal dollars') from None


def _text(value):
    return format(value, 'f')


def _at(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise SharedPaperError('A valid paper evaluation clock is required')
    return float(value)


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SharedPaperError('A bounded strategy identity is required')
    return value


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _config(raw):
    if not isinstance(raw, dict) or raw.get('kind') != 'kalshi_neutral_paper_config' or raw.get('version') != 1:
        raise SharedPaperError('Saved shared paper strategy configuration is invalid')
    sizing = dict(raw['sizing'])
    if sizing.pop('kind', None) != 'kalshi_neutral_sizing' or sizing.pop('version', None) != 1:
        raise SharedPaperError('Saved shared paper sizing configuration is invalid')
    values = {key: value for key, value in raw.items() if key not in ('kind', 'version', 'sizing')}
    return NeutralPaperConfig(sizing=NeutralSizingConfig(**sizing), **values)


def _dump_engine(engine):
    _validate(engine)
    return _json({key: value for key, value in engine.__dict__.items() if key != 'config'})


def _load_engine(config_json, state_json, state_sha):
    if _digest(state_json) != state_sha:
        raise SharedPaperError('Shared paper checkpoint integrity check failed')
    config = _config(json.loads(config_json))
    engine = NeutralPaperEngine(config)
    state = json.loads(state_json, object_hook=_decode)
    if not isinstance(state, dict) or set(state) != set(engine.__dict__)-{'config'}:
        raise SharedPaperError('Shared paper checkpoint schema differs')
    engine.__dict__.update(state)
    _validate(engine)
    return engine


def _contract_key(contract):
    return tuple(contract[key] for key in ('underlying', 'source', 'settlement_statistic',
        'window_start', 'window_end', 'payout', 'comparator'))


class SharedPaperCoordinator:
    """Serialize paper admissions and ledger updates across processes."""

    def __init__(self, path, *, capital_limit='100', loss_limit='100',
                 unhedged_loss_limit='25', timeout_seconds=5):
        self.capital = _amount(capital_limit, positive=True)
        self.loss = _amount(loss_limit)
        self.unhedged = _amount(unhedged_loss_limit)
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise SharedPaperError('SQLite wait must be finite and at most thirty seconds')
        from kalshi_bot import _private_path
        try:
            self.path = _private_path(Path(path))
            if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
                raise ValueError()
        except Exception:
            raise SharedPaperError('Use a separate regular shared paper journal outside source checkouts') from None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = self.path.exists()
        self.policy = _json(dict(capital_limit=_text(self.capital), loss_limit=_text(self.loss),
                                 unhedged_loss_limit=_text(self.unhedged)))
        self.connection = sqlite3.connect(str(self.path), timeout=timeout_seconds, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('BEGIN IMMEDIATE')
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables and not existed:
                schema = '''
                    CREATE TABLE shared_paper_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE shared_paper_strategies(
                        id TEXT PRIMARY KEY,config_json TEXT NOT NULL,config_sha TEXT NOT NULL,
                        state_json TEXT NOT NULL,state_sha TEXT NOT NULL);
                    CREATE TABLE shared_paper_intents(
                        id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,created_at REAL NOT NULL,
                        status TEXT NOT NULL,action TEXT NOT NULL,legs_json TEXT NOT NULL,
                        plan_json TEXT NOT NULL,decision_json TEXT NOT NULL,result_json TEXT);
                    CREATE TABLE shared_paper_audit(
                        id INTEGER PRIMARY KEY,at REAL NOT NULL,strategy_id TEXT,kind TEXT NOT NULL,value_json TEXT NOT NULL);
                    CREATE TABLE shared_paper_books(
                        ticker TEXT NOT NULL,quote_time REAL NOT NULL,book_hash TEXT NOT NULL,
                        PRIMARY KEY(ticker,quote_time,book_hash));
                    CREATE TABLE shared_paper_routes(ticker TEXT PRIMARY KEY,route_json TEXT NOT NULL);
                    CREATE TABLE shared_paper_quote_bindings(ticker TEXT NOT NULL,quote_time REAL NOT NULL,
                        book_hash TEXT NOT NULL,PRIMARY KEY(ticker,quote_time));
                    CREATE TABLE shared_paper_bindings(strategy_id TEXT PRIMARY KEY,binding_json TEXT NOT NULL);
                '''
                for statement in schema.split(';'):
                    if statement.strip():
                        self.connection.execute(statement)
                self.connection.executemany('INSERT INTO shared_paper_meta VALUES (?,?)',
                    dict(kind=KIND, policy=self.policy, mode='paper').items())
            self._check_policy()
            self.connection.commit()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception as error:
            self.connection.rollback()
            self.close()
            if isinstance(error, SharedPaperError):
                raise
            raise SharedPaperError('The shared paper journal is unavailable, busy or unrelated') from None

    def _check_policy(self):
        meta = dict(self.connection.execute('SELECT key,value FROM shared_paper_meta'))
        if meta.get('kind') != KIND or meta.get('mode') != 'paper' or meta.get('policy') != self.policy:
            raise SharedPaperError('Shared paper journal kind or immutable policy differs')
        return meta

    def _begin(self):
        if self.connection is None or self.connection.in_transaction:
            raise SharedPaperError('The shared paper connection is closed or already in a transaction')
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            self._check_policy()
        except Exception:
            self.connection.rollback()
            raise SharedPaperError('The shared paper journal is busy or its policy changed') from None

    def _engines(self):
        rows = list(self.connection.execute('SELECT * FROM shared_paper_strategies ORDER BY id LIMIT 65'))
        if len(rows) > 64:
            raise SharedPaperError('Shared paper strategy bound exceeded')
        result = {}
        for row in rows:
            if _digest(row['config_json']) != row['config_sha']:
                raise SharedPaperError('Shared paper strategy policy integrity check failed')
            result[row['id']] = _load_engine(row['config_json'], row['state_json'], row['state_sha'])
        routes = {row[0] for row in self.connection.execute('SELECT ticker FROM shared_paper_routes')}
        if any(not set(engine._known_contracts) <= routes for engine in result.values()):
            raise SharedPaperError('A saved strategy lacks its durable market routing identity')
        return result

    def _active(self):
        invalid = self.connection.execute("SELECT 1 FROM shared_paper_intents WHERE status NOT IN "
            "('prepared','unknown','filled','partial','unfilled') LIMIT 1").fetchone()
        if invalid is not None:
            raise SharedPaperError('A shared intent has an unrecognized lifecycle status')
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM shared_paper_intents WHERE status IN ('prepared','unknown') ORDER BY id LIMIT 1001")]
        if len(rows) > 1000:
            raise SharedPaperError('Shared pending intent bound exceeded')
        for row in rows:
            row['legs'] = json.loads(row['legs_json'])
        return rows

    def _audit(self, at, strategy, kind, value):
        self.connection.execute('INSERT INTO shared_paper_audit(at,strategy_id,kind,value_json) VALUES (?,?,?,?)',
                                (at, strategy, kind, _json(value)))

    def _global(self, engines):
        basis = sum((Decimal(lot['premium'])+Decimal(lot['fees'])
                     for engine in engines.values() for lot in engine._open_lots()), ZERO)
        realized = sum((engine.realized_pnl for engine in engines.values()), ZERO)
        rows = self._active()
        self._check_pending(engines, rows)
        reserved = sum((Decimal(leg['reservation']) for row in rows for leg in row['legs']), ZERO)
        cash = self.capital+realized-basis
        if cash < 0 or reserved > cash+Decimal('0.000000000000000001'):
            raise SharedPaperError('Shared paper cash or reservations do not reconcile')
        return dict(kind='kalshi_shared_paper_state', version=1, mode='paper', execution_eligible=False,
            source=self._check_policy().get('source'),
            observed_at=float(self._check_policy().get('last_evaluated_at', '0')) or None,
            initial_capital=_text(self.capital), cash=_text(cash), open_entry_basis=_text(basis),
            realized_pnl=_text(realized), reserved_cash=_text(reserved), available_cash=_text(cash-reserved),
            shared_committed_capital=_text(basis+reserved), active_intents=len(rows),
            unknown_intents=sum(row['status'] == 'unknown' for row in rows),
            limits=json.loads(self.policy),
            strategies=[dict(id=key, positions=dict(engine.positions), pending=engine._pending is not None,
                realized_pnl=_text(engine.realized_pnl), open_entry_basis=_text(sum(
                    (Decimal(lot['premium'])+Decimal(lot['fees']) for lot in engine._open_lots()), ZERO)))
                for key, engine in sorted(engines.items())],
            limitations=['One authoritative PAPER ledger; no exchange or live account is accessed.',
                'Strategy model bankrolls are references, not separate deposits into the shared cash pool.',
                'Unknown intent reservations are retained until explicit reconciliation; no timeout releases them.'])

    def _check_pending(self, engines, rows):
        active = {row['id']: row for row in rows}
        seen = set()
        for strategy, engine in engines.items():
            pending = engine._pending
            if pending is None:
                continue
            intent_id = pending.get('shared_intent_id')
            row = active.get(intent_id)
            if row is None or row['strategy_id'] != strategy or row['action'] != pending['action']:
                raise SharedPaperError('Shared pending checkpoints and durable intents disagree')
            legs = row['legs']
            expected = pending['legs']
            if len(legs) != len(expected):
                raise SharedPaperError('Shared pending leg identities disagree')
            for durable, leg in zip(legs, expected):
                if (durable['market'] != leg['ticker'] or durable['outcome'] != leg['outcome']
                        or Decimal(durable['quantity']) != leg['count']
                        or Decimal(durable['limit_price']) != Decimal(leg['limit_price'])):
                    raise SharedPaperError('Shared pending leg identities disagree')
                reserved = _amount(durable['reservation'])
                if reserved < _amount(durable['premium_bound'])+_amount(durable['fee_allowance']):
                    raise SharedPaperError('A durable shared leg is not fully reserved')
            if sum((Decimal(leg['reservation']) for leg in legs), ZERO) < Decimal(pending['cash_reservation']):
                raise SharedPaperError('Shared durable reservation is below the queued cash bound')
            seen.add(intent_id)
        if seen != set(active):
            raise SharedPaperError('A shared intent has no authoritative pending checkpoint')

    def _bind_routes(self, raw):
        contracts = raw.get('contracts')
        if not isinstance(contracts, list) or not 1 <= len(contracts) <= 6:
            raise SharedPaperError('A bounded shared paper contract list is required')
        for row in contracts:
            if not isinstance(row, dict):
                raise SharedPaperError('Each shared paper contract requires a routing identity')
            ticker = row.get('ticker')
            if not isinstance(ticker, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', ticker):
                raise SharedPaperError('An exact shared market ticker is required')
            index, event = row.get('exchange_index'), row.get('event_ticker')
            if raw['synthetic']:
                index = '<synthetic-unscoped>' if index is None else index
                event = '<synthetic-unscoped>' if event is None else event
            elif (type(index) is not int or not 0 <= index <= 1000000
                    or not isinstance(event, str) or not event):
                raise SharedPaperError('Public paper contracts need explicit exchange and event routing')
            route = _json(dict(exchange_index=index, event_ticker=event,
                fee_type=row.get('fee_type'), fee_multiplier=str(Decimal(str(row.get('fee_multiplier'))).normalize())))
            previous = self.connection.execute('SELECT route_json FROM shared_paper_routes WHERE ticker=?', (ticker,)).fetchone()
            if previous is not None and previous[0] != route:
                raise SharedPaperError('A shared paper market changed its exchange or event routing')
            self.connection.execute('INSERT OR IGNORE INTO shared_paper_routes VALUES (?,?)', (ticker, route))

    def _bind_quotes(self, frame, at):
        self.connection.execute('DELETE FROM shared_paper_quote_bindings WHERE quote_time<?', (at-5,))
        for row in frame['contracts']:
            books = [[[str(Decimal(price).normalize()), str(Decimal(quantity).normalize())]
                      for price, quantity in row[side]] for side in ('yes_bids', 'no_bids')]
            digest = _digest(_json(books))
            old = self.connection.execute('SELECT book_hash FROM shared_paper_quote_bindings '
                'WHERE ticker=? AND quote_time=?', (row['ticker'], row['quote_time'])).fetchone()
            if old is not None and old[0] != digest:
                raise SharedPaperError('Conflicting shared books have the same receipt timestamp')
            self.connection.execute('INSERT OR IGNORE INTO shared_paper_quote_bindings VALUES (?,?,?)',
                                    (row['ticker'], row['quote_time'], digest))

    def _scenarios(self, engines, frame):
        contracts = {}
        for engine in engines.values():
            for ticker, contract in engine._known_contracts.items():
                if ticker in contracts and contracts[ticker]['contract_identity'] != contract['contract_identity']:
                    raise SharedPaperError('Shared strategies disagree on a contract settlement identity')
                contracts[ticker] = contract
        for contract in frame['contracts']:
            ticker = contract['ticker']
            if ticker in contracts and contracts[ticker]['contract_identity'] != contract['contract_identity']:
                raise SharedPaperError('Incoming shared contract settlement identity changed')
            contracts[ticker] = contract
        return contracts

    def _state(self, engines, frame, at):
        holdings = [lot for engine in engines.values() for lot in engine._open_lots()]
        active = self._active()
        if any(row['action'] != 'open' for row in active):
            raise SharedPaperError('Risk additions wait until shared reduce-only intents are resolved')
        orders = []
        for row in active:
            for index, leg in enumerate(row['legs']):
                orders.append(dict(id=row['id']+':'+str(index), market=leg['market'], side=leg['outcome'],
                    remaining_quantity=leg['quantity'], limit_price=leg['limit_price'],
                    premium_bound=leg['premium_bound'], fee_allowance=leg['fee_allowance'],
                    reservation=leg['reservation'], status='unknown' if row['status'] == 'unknown' else 'sending'))
        state = dict(kind='kalshi_portfolio_admission_state', version=1, observed_at=at,
                     complete=True, holdings=holdings, working_orders=orders)
        return state

    def _attach_states(self, state, proposal, contracts):
        markets = {row['market'] for row in state['holdings']+state['working_orders']+proposal['legs']}
        if len(markets) > 12 or not markets <= contracts.keys():
            raise SharedPaperError('Shared payoff scenarios exceed the supported market bound')
        groups = {}
        for market in sorted(markets):
            contract = contracts[market]
            if contract['comparator'] not in ('greater', 'greater_or_equal'):
                raise SharedPaperError('Unsupported shared threshold comparator')
            groups.setdefault(_contract_key(contract), []).append(contract)
        combinations = [dict()]
        for rows in groups.values():
            rows.sort(key=lambda row: Decimal(row['strike']))
            # Equal strikes have the same event; never fabricate intervals
            # between contracts with identical cutpoints.
            strikes = sorted({Decimal(row['strike']) for row in rows})
            alternatives = [{row['ticker']: int(Decimal(row['strike']) < cut)
                             for row in rows} for cut in strikes]
            alternatives.append({row['ticker']: 1 for row in rows})
            combinations = [dict(prior, **alternative) for prior in combinations for alternative in alternatives]
            if len(combinations) > 4096:
                raise SharedPaperError('Shared payoff state bound exceeded')
        state.update(scenarios=[dict(name='shared_'+str(i), outcomes=row)
                                for i, row in enumerate(combinations)], exhaustive=True)
        return state

    def _proposal(self, plan, by_ticker, engine, at):
        legs = []
        for leg in plan.get('legs', []):
            if leg['action'] != 'buy':
                raise SharedPaperError('Purchase admission cannot reinterpret a reduction')
            contract = by_ticker[leg['ticker']]
            config = ValueSizingConfig(fee_type=contract['fee_type'], fee_multiplier=contract['fee_multiplier'],
                                       fee_quantum=Decimal('.01'))
            quantity, price = Decimal(leg['count']), Decimal(leg['limit_price'])
            fee = max(Decimal(leg['fee']), quantity*(worst_value_buy_unit_cost(price, config)-price))
            gross = Decimal(leg['gross'])
            slippage = quantity*engine.config.sizing.slippage_per_contract
            legs.append(dict(market=leg['ticker'], outcome=leg['outcome'], quantity=str(leg['count']),
                limit_price=str(leg['limit_price']), gross_cost=_text(gross), fee_allowance=_text(fee),
                slippage_allowance=_text(slippage), all_in_cost=_text(gross+fee+slippage)))
        return dict(legs=legs, observed_at=at)

    def _admit(self, strategy, engines, engine, plan, frame, by_ticker, at, failpoint):
        if plan['action'] != 'open':
            for leg in plan['legs']:
                old = engine.positions.get(leg['ticker'], 0)
                if (leg['action'] != 'sell' or not old or leg['count'] > abs(old)
                        or (old > 0) != (leg['outcome'] == 'yes')):
                    raise SharedPaperError('A shared paper reduction must sell confirmed owned contracts')
            accepted = []
            for leg in plan['legs']:
                contract = by_ticker[leg['ticker']]
                fee = (Decimal('.07')*Decimal(contract['fee_multiplier'])/4+Decimal('.0101'))*leg['count']
                accepted.append(dict(market=leg['ticker'], outcome=leg['outcome'], quantity=str(leg['count']),
                    limit_price=leg['limit_price'], premium_bound='0', fee_allowance=_text(fee), reservation=_text(fee)))
            shared = self._global(engines)
            if sum((Decimal(leg['reservation']) for leg in accepted), ZERO) > Decimal(shared['available_cash']):
                return dict(status='hold', action='hold', legs=[], reason='Shared cash cannot reserve reduction fees')
            decision = dict(decision='allow', reason='Verified reduce-only paper plan', execution_eligible=False)
        else:
            original_markets = {(leg['ticker'], leg['outcome']) for leg in plan['legs']}
            original_counts = {(leg['ticker'], leg['outcome']): leg['count'] for leg in plan['legs']}
            original_limits = {(leg['ticker'], leg['outcome']): Decimal(leg['limit_price']) for leg in plan['legs']}
            contracts = self._scenarios(engines, frame)
            state = self._state(engines, frame, at)
            realized = sum((item.realized_pnl for item in engines.values()), ZERO)
            effective = min(self.capital, self.capital+realized)
            if effective <= 0:
                return dict(status='hold', action='hold', legs=[], reason='No shared paper capital remains')
            for _ in range(3):
                proposal = self._proposal(plan, by_ticker, engine, frame['observed_at'])
                joined = self._attach_states(deepcopy(state), proposal, contracts)
                decision = assess_admission(joined, proposal, now=at, capital_limit=_text(effective),
                    loss_limit=_text(self.loss), unhedged_loss_limit=_text(self.unhedged))
                if decision['decision'] == 'allow':
                    accepted = decision['accepted_proposal']['legs']
                    break
                if decision['decision'] != 'resize':
                    self._audit(at, strategy, 'admission_block', decision)
                    return dict(status='hold', action='hold', legs=[], reason=decision['reason'], shared_admission=decision)
                approved = decision['accepted_proposal']['legs']
                count = max(int(leg['quantity']) for leg in approved)
                cost = sum((Decimal(leg['reservation']) for leg in approved), ZERO)
                sizing = replace(engine.config.sizing, max_leg_contracts=count,
                                 max_pair_cost=min(engine.config.sizing.max_pair_cost, cost))
                inputs = dict(frame, cash=_text(engine.cash), bankroll=_text(engine.bankroll),
                              positions=dict(engine.positions), force_reduce=engine._force_reduce)
                replanned = plan_neutral_spread(inputs, sizing)
                if (replanned.get('status') != 'proposed' or replanned.get('action') != 'open'
                        or {(leg['ticker'], leg['outcome']) for leg in replanned['legs']} != original_markets
                        or any(leg['count'] > original_counts[(leg['ticker'], leg['outcome'])]
                               or Decimal(leg['limit_price']) > original_limits[(leg['ticker'], leg['outcome'])]
                               for leg in replanned['legs'])):
                    return dict(status='hold', action='hold', legs=[], reason='Shared resize requires a freshly feasible same-pair plan')
                plan = replanned
            else:
                return dict(status='hold', action='hold', legs=[], reason='Shared resize did not converge to an admitted plan')
        intent_id = uuid.uuid4().hex
        self.connection.execute('INSERT INTO shared_paper_intents '
            '(id,strategy_id,created_at,status,action,legs_json,plan_json,decision_json,result_json) '
            'VALUES (?,?,?,?,?,?,?,?,NULL)',
            (intent_id, strategy, at, 'prepared', plan['action'], _json(accepted), _json(plan), _json(decision)))
        # Explicit columns avoid schema-coupled accidental reservation changes.
        self._audit(at, strategy, 'intent_prepared', dict(intent_id=intent_id, decision=decision))
        if failpoint is not None:
            failpoint('after_admission')
        return dict(plan, shared_intent_id=intent_id, shared_admission=decision)

    def _sync(self, strategy, engine):
        pending = engine._pending
        pending_id = None if pending is None else pending.get('shared_intent_id')
        actions = {row.get('shared_intent_id'): row for row in engine.actions if row.get('shared_intent_id')}
        for row in self._active():
            if row['strategy_id'] != strategy:
                continue
            if row['id'] == pending_id:
                continue
            if row['status'] == 'unknown':
                raise SharedPaperError('Unknown paper intent cannot be released by an inferred timeout')
            result = actions.get(row['id'])
            if result is None or result.get('status') not in ('filled', 'partial', 'unfilled'):
                raise SharedPaperError('A durable paper intent has no matching pending or completion evidence')
            self.connection.execute('UPDATE shared_paper_intents SET status=?,result_json=? WHERE id=?',
                (result['status'], _json(result), row['id']))
        if pending is not None:
            row = self.connection.execute('SELECT * FROM shared_paper_intents WHERE id=?', (pending_id,)).fetchone()
            if row is None or row['strategy_id'] != strategy or row['status'] not in _ACTIVE:
                raise SharedPaperError('A queued paper order lacks its atomic durable shared intent')

    def _save(self, strategy, engine):
        state = _dump_engine(engine)
        config = _json(engine.config.to_dict())
        self.connection.execute('INSERT INTO shared_paper_strategies VALUES (?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json,state_sha=excluded.state_sha',
            (strategy, config, _digest(config), state, _digest(state)))

    def step(self, strategy_id, raw, config=None, *, now=None, failpoint=None, experiment_binding=None):
        strategy_id, at = _identity(strategy_id), _at(time.time() if now is None else now)
        base_at = at
        started = time.monotonic()
        public = isinstance(raw, dict) and raw.get('synthetic') is False
        def evaluation_clock():
            if now is None:
                return _at(time.time())
            return _at(base_at + max(0, time.monotonic()-started)) if public else base_at
        if failpoint is not None and not callable(failpoint):
            raise SharedPaperError('A fault injection hook must be callable')
        self._begin()
        try:
            at = evaluation_clock()
            engines = self._engines()
            if strategy_id in engines:
                engine = engines[strategy_id]
                if config is not None and _json(config.to_dict()) != _json(engine.config.to_dict()):
                    raise SharedPaperError('The strategy policy changed; use a separate strategy identity')
            else:
                if len(engines) >= 64:
                    raise SharedPaperError('Shared paper strategy bound exceeded')
                config = NeutralPaperConfig() if config is None else config
                if not isinstance(config, NeutralPaperConfig) or config.initial_cash > self.capital:
                    raise SharedPaperError('A strategy reference bankroll cannot exceed the shared allocation')
                engine = engines[strategy_id] = NeutralPaperEngine(config)
            prior_binding = self.connection.execute('SELECT binding_json FROM shared_paper_bindings '
                                                    'WHERE strategy_id=?', (strategy_id,)).fetchone()
            if experiment_binding is not None or prior_binding is None:
                binding = _json(experiment_binding)
                if len(binding) > 64000:
                    raise SharedPaperError('Shared strategy experiment binding exceeds its bound')
                if prior_binding is not None and prior_binding[0] != binding:
                    raise SharedPaperError('The shared strategy source or model policy changed')
                self.connection.execute('INSERT OR IGNORE INTO shared_paper_bindings VALUES (?,?)',
                                        (strategy_id, binding))
            if not isinstance(raw, dict) or type(raw.get('synthetic')) is not bool:
                raise SharedPaperError('Shared paper source must explicitly identify synthetic data')
            source = 'synthetic' if raw['synthetic'] else 'public'
            meta = self._check_policy()
            if meta.get('source', source) != source:
                raise SharedPaperError('Synthetic and public strategies require separate shared journals')
            self.connection.execute('INSERT OR IGNORE INTO shared_paper_meta VALUES (?,?)', ('source', source))
            self._bind_routes(raw)
            last = float(meta.get('last_evaluated_at', '0'))
            if at < last:
                raise SharedPaperError('The shared paper evaluation clock moved backwards')
            normalized = normalize_neutral_snapshot(raw, now=at)
            self._scenarios(engines, normalized)
            self._bind_quotes(normalized, at)
            if any(row['strategy_id'] == strategy_id and row['status'] == 'unknown' for row in self._active()):
                result = dict(kind='kalshi_shared_paper_step', version=1, execution_eligible=False,
                    strategy=engine.snapshot(), shared=self._global(engines),
                    admission=dict(decision='block', reason='An unknown intent retains its full shared reservation'))
                self.connection.commit()
                return result
            self.connection.execute('DELETE FROM shared_paper_books WHERE quote_time<?', (at-5,))
            consumed = {tuple(row) for row in self.connection.execute('SELECT ticker,quote_time,book_hash FROM shared_paper_books')}
            if len(consumed) > 10000:
                raise SharedPaperError('Shared consumed-book bound exceeded')
            engine._consumed_books |= consumed
            # Synchronize completed old intents before evaluating a new plan;
            # the callback sees holdings updated by prior simulated fills.
            admission_results = []
            def admit(plan, frame, by_ticker):
                self._sync(strategy_id, engine)
                checked = evaluation_clock()
                if public and checked-frame['observed_at'] > 5:
                    result = dict(status='hold', action='hold', legs=[],
                                  reason='The public paper frame became stale during planning')
                else:
                    result = self._admit(strategy_id, engines, engine, plan, frame, by_ticker, checked, failpoint)
                admission_results.append(result.get('shared_admission',
                    dict(decision='block', reason=result.get('reason'))))
                return result
            original_apply = engine._apply_fill
            applied = [False]
            if failpoint is not None:
                def apply(_engine, *args):
                    original_apply(*args)
                    if not applied[0]:
                        applied[0] = True
                        failpoint('after_first_leg')
                engine._apply_fill = types.MethodType(apply, engine)
            try:
                strategy = engine.step(raw, now=evaluation_clock(), admission=admit)
            finally:
                engine.__dict__.pop('_apply_fill', None)
            self._sync(strategy_id, engine)
            if failpoint is not None:
                failpoint('before_checkpoint')
            committed_at = evaluation_clock()
            if (public and any(row.get('decision') in ('allow', 'resize') for row in admission_results)
                    and committed_at-raw['observed_at'] > 5):
                raise SharedPaperError('Public paper quotes became stale before intent commit')
            self._save(strategy_id, engine)
            for key in engine._consumed_books:
                self.connection.execute('INSERT OR IGNORE INTO shared_paper_books VALUES (?,?,?)', key)
            self.connection.execute('INSERT OR REPLACE INTO shared_paper_meta VALUES (?,?)', ('last_evaluated_at', str(committed_at)))
            shared = self._global(engines)
            if Decimal(shared['shared_committed_capital']) > self.capital:
                raise SharedPaperError('The committed paper portfolio exceeds its shared allocation')
            self._audit(at, strategy_id, 'step_committed', dict(shared=shared, admission=admission_results))
            if failpoint is not None:
                failpoint('before_commit')
            self.connection.commit()
            return dict(kind='kalshi_shared_paper_step', version=1, execution_eligible=False,
                        strategy=strategy, shared=shared,
                        admission=admission_results[-1] if admission_results else None)
        except Exception as error:
            self.connection.rollback()
            if isinstance(error, SharedPaperError):
                raise
            raise SharedPaperError('The shared paper transaction failed; no changes were committed') from None

    def snapshot(self):
        if self.connection is None:
            raise SharedPaperError('The shared paper journal is closed')
        try:
            self.connection.execute('BEGIN')
            self._check_policy()
            report = self._global(self._engines())
            self.connection.commit()
            return report
        except Exception as error:
            self.connection.rollback()
            if isinstance(error, SharedPaperError):
                raise
            raise SharedPaperError('The shared paper snapshot is invalid') from None

    def strategy_snapshot(self, strategy_id):
        strategy_id = _identity(strategy_id)
        try:
            self.connection.execute('BEGIN')
            self._check_policy()
            engine = self._engines().get(strategy_id)
            result = None if engine is None else engine.snapshot()
            self.connection.commit()
            return result
        except Exception as error:
            self.connection.rollback()
            if isinstance(error, SharedPaperError):
                raise
            raise SharedPaperError('The shared strategy snapshot is invalid') from None

    def mark_unknown(self, intent_id):
        """Quarantine a durable pending intent; never releases its reservation."""
        self._begin()
        try:
            row = self.connection.execute('SELECT * FROM shared_paper_intents WHERE id=?', (intent_id,)).fetchone()
            if row is None or row['status'] not in _ACTIVE:
                raise SharedPaperError('Only an active durable intent can become unknown')
            self.connection.execute("UPDATE shared_paper_intents SET status='unknown' WHERE id=?", (intent_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _ledger_action(self, strategy_id, action, at):
        strategy_id = _identity(strategy_id)
        self._begin()
        try:
            if at < float(self._check_policy().get('last_evaluated_at', '0')):
                raise SharedPaperError('The shared paper evaluation clock moved backwards')
            engines = self._engines()
            engine = engines.get(strategy_id)
            if engine is None:
                raise SharedPaperError('No coordinated paper strategy exists under that identity')
            if any(row['strategy_id'] == strategy_id and row['status'] == 'unknown' for row in self._active()):
                raise SharedPaperError('Unknown shared paper intent requires explicit reconciliation')
            evidence = action(engine)
            self._sync(strategy_id, engine)
            self._save(strategy_id, engine)
            self.connection.execute('INSERT OR REPLACE INTO shared_paper_meta VALUES (?,?)',
                                    ('last_evaluated_at', str(at)))
            shared = self._global(engines)
            self._audit(at, strategy_id, 'ledger_action', evidence)
            self.connection.commit()
            return dict(kind='kalshi_shared_paper_step', version=1, execution_eligible=False,
                        strategy=engine.snapshot(), shared=shared, admission=None, event=evidence)
        except Exception as error:
            self.connection.rollback()
            if isinstance(error, SharedPaperError):
                raise
            raise SharedPaperError('The shared paper ledger action failed; no changes were committed') from None

    def settle(self, strategy_id, market_payload, *, received_at):
        at = _at(received_at)
        return self._ledger_action(strategy_id, lambda engine: engine.settle(market_payload, received_at=at), at)

    def expire_pending(self, strategy_id, *, now):
        at = _at(now)
        return self._ledger_action(strategy_id, lambda engine: engine.expire_pending(now=at), at)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
