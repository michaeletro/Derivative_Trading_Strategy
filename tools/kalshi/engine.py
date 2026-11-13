"""Bounded Kalshi event-contract runner with a separate durable attempt journal.

Only caller-supplied event probabilities are traded. Paper fills are a top-of-book
simulation, not an exchange fill model. Reserved estimated cost is cumulative: no
automatic release, daily reset, account liquidation or uncertain-write retry.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

ONE = Decimal("1")
ZERO = Decimal("0")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class EngineError(ValueError):
    """Sanitized invalid configuration, state or trading input."""


def decimal_value(value, name="decimal", *, places=4, minimum=ZERO, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise EngineError("Invalid " + name)
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise EngineError("Invalid " + name) from None
    if (not result.is_finite() or result < minimum
            or (maximum is not None and result > maximum)
            or result.as_tuple().exponent < -places):
        raise EngineError("Invalid " + name)
    return result


def _integer(value, name, low=1, high=10_000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EngineError("Invalid " + name)
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise EngineError("Invalid UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise EngineError("Invalid UTC timestamp") from None


def _iso(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat()


def _identifier(value):
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise EngineError("Invalid signal or market identifier")
    return value


@dataclass(frozen=True)
class RiskConfig:
    max_total_cost: Decimal = Decimal("50")
    max_order_cost: Decimal = Decimal("5")
    max_market_cost: Decimal = Decimal("10")
    max_contracts: int = 5
    min_edge: Decimal = Decimal("0.03")
    fee_reserve_per_contract: Decimal = Decimal("0.02")
    max_quote_age_seconds: float = 5
    max_signal_age_seconds: float = 60
    min_seconds_to_close: float = 60
    sizing_mode: str = "fixed"
    kelly_fraction: Decimal = Decimal("0.25")
    kelly_bankroll: Decimal = Decimal("50")
    required_series: str | None = None

    def __post_init__(self):
        for name in ("max_total_cost", "max_order_cost", "max_market_cost"):
            value = decimal_value(getattr(self, name), name, maximum=Decimal("1000000"))
            if value <= ZERO:
                raise EngineError("Risk budgets must be positive")
            object.__setattr__(self, name, value)
        if self.max_order_cost > self.max_total_cost or self.max_market_cost > self.max_total_cost:
            raise EngineError("Order and market budgets cannot exceed total budget")
        for name in ("min_edge", "fee_reserve_per_contract"):
            value = decimal_value(getattr(self, name), name, maximum=ONE)
            if value <= ZERO:
                raise EngineError("Edge and fee reserve must be positive")
            object.__setattr__(self, name, value)
        _integer(self.max_contracts, "maximum contracts")
        if not isinstance(self.sizing_mode, str) or self.sizing_mode not in ("fixed", "kelly"):
            raise EngineError("Invalid sizing mode")
        fraction = decimal_value(self.kelly_fraction, "Kelly fraction", places=6, maximum=ONE)
        bankroll = decimal_value(self.kelly_bankroll, "Kelly bankroll", maximum=Decimal("1000000"))
        if fraction <= ZERO or bankroll <= ZERO:
            raise EngineError("Kelly fraction and bankroll must be positive")
        object.__setattr__(self, "kelly_fraction", fraction)
        object.__setattr__(self, "kelly_bankroll", bankroll)
        if self.required_series is not None:
            _identifier(self.required_series)
        for name in ("max_quote_age_seconds", "max_signal_age_seconds", "min_seconds_to_close"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 86400:
                raise EngineError("Invalid freshness or expiry bound")
            object.__setattr__(self, name, float(value))

    def to_dict(self):
        return {field.name: (format(getattr(self, field.name), "f") if isinstance(getattr(self, field.name), Decimal)
                             else getattr(self, field.name)) for field in fields(self)}


def validate_signals(payload, risk, now):
    if not isinstance(payload, dict) or set(payload) != {"kind", "version", "generated_at", "signals"}:
        raise EngineError("Invalid signal document schema")
    if payload["kind"] != "kalshi_signals" or type(payload["version"]) is not int or payload["version"] != 1:
        raise EngineError("Unsupported signal document")
    age = now - _timestamp(payload["generated_at"])
    if age < -5 or age > risk.max_signal_age_seconds:
        raise EngineError("Signal document is stale or future dated")
    signals = payload["signals"]
    if not isinstance(signals, list) or not 1 <= len(signals) <= 100:
        raise EngineError("Signal document must contain 1 to 100 signals")
    ids, tickers, validated = set(), set(), []
    for item in signals:
        if not isinstance(item, dict) or set(item) != {"signal_id", "ticker", "probability_yes", "max_count"}:
            raise EngineError("Invalid signal schema")
        signal_id = _identifier(item["signal_id"])
        ticker = _identifier(item["ticker"])
        if signal_id in ids or ticker in tickers:
            raise EngineError("Duplicate signal identifier or ticker")
        ids.add(signal_id)
        tickers.add(ticker)
        probability = decimal_value(item["probability_yes"], "event probability", places=6, maximum=ONE)
        count = _integer(item["max_count"], "signal contract ceiling")
        validated.append(dict(signal_id=signal_id, ticker=ticker, probability_yes=probability, max_count=count))
    return validated


def _execution_metadata(value, count):
    required = {'order_type', 'time_in_force', 'expiration_time', 'remaining_count', 'cancel_requested'}
    if not isinstance(value, dict) or set(value) != required:
        raise EngineError("Invalid intent execution metadata")
    order_type = value['order_type']
    expected = {'ioc': 'immediate_or_cancel', 'limit': 'good_till_canceled'}
    if not isinstance(order_type, str) or order_type not in expected or value['time_in_force'] != expected[order_type]:
        raise EngineError("Invalid intent order type")
    expiry = value['expiration_time']
    if ((order_type == 'ioc' and expiry is not None) or
            (order_type == 'limit' and (type(expiry) is not int or not 0 < expiry <= 253402300799))):
        raise EngineError("Invalid intent expiration")
    if type(value['cancel_requested']) is not bool:
        raise EngineError("Invalid cancellation state")
    remaining = decimal_value(value['remaining_count'], 'remaining quantity', places=2, maximum=count)
    if remaining != remaining.to_integral_value():
        raise EngineError("Intent quantities must be whole contracts")
    return dict(value, remaining_count=format(remaining, '.2f'))


class Journal:
    """A dedicated SQLite file. Existing non-Kalshi files are refused."""

    def __init__(self, path, mode="paper", environment="demo", risk=None):
        if mode not in ("paper", "demo", "live") or environment not in ("demo", "production"):
            raise EngineError("Invalid Kalshi mode or environment")
        if (mode == "demo" and environment != "demo") or (mode == "live" and environment != "production"):
            raise EngineError("Trading mode does not match environment")
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise EngineError("Kalshi journal paths cannot contain symbolic links")
        self.path = original.resolve()
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise EngineError("Kalshi journal cannot have hard-link aliases")
        self.lock_path = Path(str(self.path) + ".lock")
        self.mode, self.environment = mode, environment
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = self.path.exists()
        self.connection = sqlite3.connect(str(self.path), timeout=0)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA synchronous=FULL")
            if existed:
                found = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kalshi_meta'").fetchone()
                if not found or self._meta("kind") != "kalshi_bot_journal_v1":
                    raise EngineError("Existing file is not a Kalshi journal")
                if self._meta("mode") != mode or self._meta("environment") != environment:
                    raise EngineError("Journal mode or environment differs")
                self.risk = RiskConfig(**json.loads(self._meta("risk")))
                if risk is not None and risk.to_dict() != self.risk.to_dict():
                    raise EngineError("Journal risk configuration differs")
            else:
                self.risk = risk or RiskConfig()
                with self.connection:
                    self.connection.executescript("""
                    CREATE TABLE kalshi_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE consumed_signals (
                        signal_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, consumed_at TEXT NOT NULL);
                    CREATE TABLE intents (
                        client_order_id TEXT PRIMARY KEY, signal_id TEXT UNIQUE NOT NULL,
                        ticker TEXT NOT NULL, outcome TEXT NOT NULL, count TEXT NOT NULL,
                        price TEXT NOT NULL, reserved_cost TEXT NOT NULL, created_at TEXT NOT NULL,
                        status TEXT NOT NULL, order_id TEXT, fill_count TEXT NOT NULL DEFAULT '0.00');
                    """)
                    for key, value in dict(kind="kalshi_bot_journal_v1", mode=mode, environment=environment,
                                           risk=json.dumps(self.risk.to_dict(), sort_keys=True)).items():
                        self.connection.execute("INSERT INTO kalshi_meta VALUES (?, ?)", (key, value))
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
            self.connection.close()
            if isinstance(error, EngineError):
                raise
            raise EngineError("Kalshi journal is unavailable or invalid") from None

    def _meta(self, key):
        row = self.connection.execute("SELECT value FROM kalshi_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def bind_risk(self, risk):
        if risk.to_dict() != self.risk.to_dict():
            raise EngineError("Journal risk configuration differs")

    @contextmanager
    def exclusive(self):
        token = str(uuid.uuid4())
        try:
            fd = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise EngineError("Kalshi journal is locked; inspect the other runner or stale lock") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                if self.lock_path.read_text(encoding="utf-8") == token:
                    self.lock_path.unlink()
            except OSError:
                pass

    def intents(self):
        result = [dict(row) for row in self.connection.execute("SELECT * FROM intents ORDER BY rowid")]
        # Legacy journals do not contain sizing evidence. Additive metadata is
        # created only when the first Kelly intent is durably recorded.
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_sizing'").fetchone():
            sizing = {row[0]: json.loads(row[1]) for row in self.connection.execute(
                "SELECT client_order_id,sizing_json FROM intent_sizing")}
            for row in result:
                if row["client_order_id"] in sizing:
                    row["sizing"] = sizing[row["client_order_id"]]
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_execution'").fetchone():
            execution = {row[0]: json.loads(row[1]) for row in self.connection.execute(
                "SELECT client_order_id,execution_json FROM intent_execution")}
            for row in result:
                if row['client_order_id'] in execution:
                    row['execution'] = _execution_metadata(execution[row['client_order_id']], Decimal(row['count']))
        return result

    def committed(self, ticker=None):
        return sum((Decimal(row["reserved_cost"]) for row in self.intents()
                    if ticker is None or row["ticker"] == ticker), ZERO)

    def halted(self):
        return bool(self.connection.execute("SELECT 1 FROM intents WHERE status IN ('sending','unknown') LIMIT 1").fetchone())

    def has_resting(self):
        return bool(self.connection.execute(
            "SELECT 1 FROM intents WHERE status IN ('resting','paper_resting','cancel_pending') LIMIT 1").fetchone())

    def consume(self, signal, now):
        fingerprint = hashlib.sha256(json.dumps({key: str(value) for key, value in signal.items()},
                                                sort_keys=True).encode()).hexdigest()
        row = self.connection.execute("SELECT fingerprint FROM consumed_signals WHERE signal_id=?", (signal["signal_id"],)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise EngineError("A consumed signal identifier was changed")
            return False
        with self.connection:
            self.connection.execute("INSERT INTO consumed_signals VALUES (?, ?, ?)",
                                    (signal["signal_id"], fingerprint, _iso(now)))
        return True

    def record(self, signal, outcome, count, price, cost, now, sizing=None, execution=None):
        order_id = str(uuid.uuid4())
        checked_execution = _execution_metadata(execution, count) if execution is not None else None
        with self.connection:
            self.connection.execute("INSERT INTO intents "
                "(client_order_id,signal_id,ticker,outcome,count,price,reserved_cost,created_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, signal["signal_id"], signal["ticker"], outcome, format(count, ".2f"),
                 format(price, ".4f"), format(cost, "f"), _iso(now), "sending"))
            if sizing is not None:
                self.connection.execute("CREATE TABLE IF NOT EXISTS intent_sizing "
                    "(client_order_id TEXT PRIMARY KEY, sizing_json TEXT NOT NULL)")
                self.connection.execute("INSERT INTO intent_sizing VALUES (?, ?)",
                    (order_id, json.dumps(sizing, sort_keys=True, allow_nan=False)))
            if checked_execution is not None:
                self.connection.execute("CREATE TABLE IF NOT EXISTS intent_execution "
                    "(client_order_id TEXT PRIMARY KEY, execution_json TEXT NOT NULL)")
                self.connection.execute("INSERT INTO intent_execution VALUES (?, ?)",
                    (order_id, json.dumps(checked_execution, sort_keys=True, allow_nan=False)))
        return order_id

    def set_execution(self, client_order_id, **updates):
        row = next((row for row in self.intents() if row['client_order_id'] == client_order_id), None)
        if row is None or 'execution' not in row:
            raise EngineError("Intent execution metadata is unavailable")
        value = _execution_metadata(dict(row['execution'], **updates), Decimal(row['count']))
        with self.connection:
            self.connection.execute('UPDATE intent_execution SET execution_json=? WHERE client_order_id=?',
                                    (json.dumps(value, sort_keys=True, allow_nan=False), client_order_id))

    def finish(self, client_order_id, status, order_id=None, fill_count=ZERO):
        with self.connection:
            self.connection.execute("UPDATE intents SET status=?,order_id=?,fill_count=? WHERE client_order_id=?",
                                    (status, order_id, format(fill_count, ".2f"), client_order_id))

    def bind_account(self, fingerprint):
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise EngineError("Authenticated account identity is unavailable")
        previous = self._meta("account_fingerprint")
        if previous is not None and previous != fingerprint:
            raise EngineError("Journal belongs to a different authenticated key")
        if previous is None:
            if self.intents():
                raise EngineError("Existing journal has no authenticated account identity")
            with self.connection:
                self.connection.execute("INSERT INTO kalshi_meta VALUES ('account_fingerprint', ?)", (fingerprint,))

    def cash_budget(self, balance):
        baseline = self._meta("initial_cash")
        if baseline is None:
            with self.connection:
                self.connection.execute("INSERT INTO kalshi_meta VALUES ('initial_cash', ?)", (format(balance, "f"),))
            baseline = format(balance, "f")
        return min(balance, max(ZERO, Decimal(baseline) - self.committed()))

    def export_report(self, decisions=None):
        return dict(kind="kalshi_bot_report", version=1, mode=self.mode, environment=self.environment,
                    generated_at=_iso(), risk=self.risk.to_dict(),
                    summary=dict(committed_cost=format(self.committed(), "f"), intent_count=len(self.intents()),
                                 halted=self.halted(), open_limit_count=sum(row['status'] in
                                    ('resting','paper_resting','cancel_pending') for row in self.intents())),
                    intents=self.intents(), decisions=list(decisions or []))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _levels(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise EngineError("Invalid fixed-point order book")
    result, seen = [], set()
    for level in value:
        if not isinstance(level, list) or len(level) != 2:
            raise EngineError("Invalid fixed-point order book")
        price = decimal_value(level[0], "book price", maximum=ONE)
        count = decimal_value(level[1], "book quantity", places=2, maximum=Decimal("100000000"))
        if price <= ZERO or price >= ONE or count <= ZERO or price in seen:
            raise EngineError("Invalid fixed-point order book")
        seen.add(price)
        result.append((price, count))
    return sorted(result)


def _valid_tick(price, ranges):
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 100:
        raise EngineError("Market price ranges are unavailable")
    valid = False
    for item in ranges:
        if not isinstance(item, dict) or not {"start", "end", "step"} <= set(item):
            raise EngineError("Invalid market price ranges")
        start = decimal_value(item["start"], "tick range start", maximum=ONE)
        end = decimal_value(item["end"], "tick range end", maximum=ONE)
        step = decimal_value(item["step"], "tick step", maximum=ONE)
        if end <= start or step <= ZERO:
            raise EngineError("Invalid market price ranges")
        if start <= price <= end and (price - start) % step == ZERO:
            valid = True
    return valid


class Bot:
    """Buy-only planner and IOC file runner; the console also uses limit planning."""

    def __init__(self, client, journal, risk=None, mode=None, clock=time.time):
        self.client, self.journal = client, journal
        self.risk, self.mode, self.clock = risk or journal.risk, mode or journal.mode, clock
        journal.bind_risk(self.risk)
        if self.mode != journal.mode:
            raise EngineError("Bot mode differs from journal")
        if getattr(client, "environment", journal.environment) != journal.environment:
            raise EngineError("Client environment differs from journal")
        self._account_positions = None
        self._account_snapshot_at = None

    def _account_cash(self):
        self._account_positions = None
        self._account_snapshot_at = None
        self.journal.bind_account(getattr(self.client, "account_fingerprint", None))
        status = self.client.get_exchange_status()
        if not isinstance(status, dict) or status.get("exchange_active") is not True or status.get("trading_active") is not True:
            raise EngineError("Exchange trading is unavailable")
        balance = self.client.get_balance()
        if not isinstance(balance, dict):
            raise EngineError("Account balance is unknown")
        if "balance_dollars" in balance:
            cash = decimal_value(balance["balance_dollars"], "cash balance", maximum=Decimal("1000000000"))
        else:
            cents = balance.get("balance")
            if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
                raise EngineError("Account balance is unknown")
            cash = Decimal(cents) / 100
        positions = self.client.get_positions()
        orders = self.client.get_orders(status="resting")
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise EngineError("Account state is incomplete")
        if orders:
            raise EngineError("Resting exchange orders exist; cancel or reconcile them before a new entry")
        owned = {}
        for row in self.journal.intents():
            if row["status"] == "filled" or (row.get('execution', {}).get('order_type') == 'limit'
                    and row['status'] == 'canceled'):
                quantity = Decimal(row["fill_count"]) * (1 if row["outcome"] == "yes" else -1)
                owned[row["ticker"]] = owned.get(row["ticker"], ZERO) + quantity
        seen, actual = set(), {}
        for row in positions:
            if not isinstance(row, dict):
                raise EngineError("Account position state is unknown")
            ticker = _identifier(row.get("ticker"))
            if ticker in seen:
                raise EngineError("Duplicate account position")
            seen.add(ticker)
            position = decimal_value(row.get("position_fp"), "account position", places=2,
                                     minimum=Decimal("-100000000"), maximum=Decimal("100000000"))
            actual[ticker] = position
            if position == ZERO:
                continue
            expected = owned.get(ticker, ZERO)
            if expected == ZERO or position * expected < ZERO or abs(position) > abs(expected):
                raise EngineError("Unowned or unsupported account exposure exists")
        cash_budget = self.journal.cash_budget(cash)
        self._account_positions = actual
        self._account_snapshot_at = self.clock()
        return cash_budget

    def _kelly_holding(self, ticker):
        """Confirmed same-side fills; pending/unknown attempts cannot be topped up.

        A canceled IOC has no holding, although its reservation remains in every
        cumulative budget. Actual account reconciliation is required separately
        before adding to a journal-owned live holding.
        """
        if self.journal.halted():
            raise EngineError("Journal has an unresolved order; submissions are halted")
        wanted_status = "paper_filled" if self.mode == "paper" else "filled"
        outcome, quantity = None, ZERO
        for row in self.journal.intents():
            eligible = row['status'] == wanted_status or (row.get('execution', {}).get('order_type') == 'limit'
                and row['status'] == ('paper_canceled' if self.mode == 'paper' else 'canceled'))
            if row["ticker"] != ticker or not eligible:
                continue
            held = decimal_value(row["fill_count"], "journal filled quantity", places=2,
                                 maximum=Decimal("100000000"))
            count = decimal_value(row["count"], "journal order quantity", places=2,
                                  maximum=Decimal("100000000"))
            if held != held.to_integral_value() or held > count or row["outcome"] not in ("yes", "no"):
                raise EngineError("Journal position requires reconciliation")
            if held == ZERO:
                continue
            if outcome is not None and outcome != row["outcome"]:
                raise EngineError("Mixed-outcome journal position requires reconciliation")
            outcome = row["outcome"]
            quantity += held
        if self.mode != "paper" and quantity > ZERO:
            age = None if self._account_snapshot_at is None else self.clock() - self._account_snapshot_at
            if self._account_positions is None or age is None or not 0 <= age <= self.risk.max_quote_age_seconds:
                raise EngineError("A fresh account position snapshot is required before adding")
            expected = quantity if outcome == "yes" else -quantity
            if self._account_positions.get(ticker, ZERO) != expected:
                raise EngineError("Account position differs from journal fills; reconcile before adding")
        return outcome, quantity

    def _plan(self, signal, cash, started, sizing_overrides=None, requested_outcome='auto', limit_price=None):
        if not isinstance(requested_outcome, str) or requested_outcome not in ('auto', 'yes', 'no'):
            raise EngineError("Choose automatic, YES or NO contract sizing")
        if self.journal.has_resting():
            raise EngineError("A limit order is still outstanding; resolve or cancel it before another entry")
        entered_price = None
        if limit_price is not None:
            entered_price = decimal_value(limit_price, 'limit price', maximum=ONE)
            if requested_outcome == 'auto' or not ZERO < entered_price < ONE:
                raise EngineError("A resting limit requires YES or NO and a price strictly between zero and one")
        # A preview may choose a smaller stake without rebinding or replacing
        # the journal's risk policy. Every other spending limit still applies.
        selected_bankroll, selected_fraction = self.risk.kelly_bankroll, self.risk.kelly_fraction
        if sizing_overrides is not None:
            if (self.risk.sizing_mode != "kelly" or not isinstance(sizing_overrides, dict)
                    or sizing_overrides.keys() - {"bankroll", "kelly_fraction"}):
                raise EngineError("Unsupported Kelly sizing overrides")
            for key, configured, places in (("bankroll", selected_bankroll, 4),
                                             ("kelly_fraction", selected_fraction, 6)):
                value = decimal_value(sizing_overrides.get(key, configured), key,
                                      places=places, maximum=configured)
                if value <= ZERO:
                    raise EngineError("Kelly selections must be positive and within startup ceilings")
                if key == "bankroll":
                    selected_bankroll = value
                else:
                    selected_fraction = value
        if len(self.journal.intents()) >= 2000:
            raise EngineError("Journal attempt limit reached")
        previous = {row["outcome"] for row in self.journal.intents() if row["ticker"] == signal["ticker"]}
        held_outcome, held_quantity = (self._kelly_holding(signal["ticker"])
                                       if self.risk.sizing_mode == "kelly" else (None, ZERO))
        if requested_outcome != 'auto' and held_outcome is not None and requested_outcome != held_outcome:
            raise EngineError("Selected contract side differs from the existing position; reconcile before changing sides")
        series = self.risk.required_series
        if series is not None and not signal["ticker"].startswith(series + "-"):
            raise EngineError("Market does not belong to the required series")
        market = self.client.get_market(signal["ticker"])
        event = None
        event_ticker = None
        btc_series = None
        if series is not None:
            if not isinstance(market, dict):
                raise EngineError("Market identity is unavailable")
            event_ticker = _identifier(market.get("event_ticker"))
            if not event_ticker.startswith(series + "-") or not signal["ticker"].startswith(event_ticker + "-"):
                raise EngineError("Market event identity differs from the required series")
            event = self.client.get_event(event_ticker)
            if series == "KXBTC15M":
                btc_series = self.client.get_series(series)
        raw = self.client.get_orderbook(signal["ticker"])
        now = self.clock()
        if now < started or now - started > self.risk.max_quote_age_seconds:
            raise EngineError("Market or account snapshot exceeded freshness bound")
        if not isinstance(market, dict) or market.get("ticker") != signal["ticker"]:
            raise EngineError("Market identity is unavailable")
        if series is not None and (not isinstance(event, dict)
                or event.get("event_ticker") != event_ticker or event.get("series_ticker") != series):
            raise EngineError("Event metadata does not establish the required series")
        if market.get("status") != "active" or market.get("market_type") != "binary":
            raise EngineError("Only active binary markets are supported")
        if market.get("settlement_bounds_type", "default") != "default":
            raise EngineError("Only standard binary settlement is supported")
        if ("notional_value_dollars" in market
                and decimal_value(market["notional_value_dollars"], "contract payout") != ONE):
            raise EngineError("Only one-dollar binary contract payouts are supported")
        if _timestamp(market.get("close_time")) - now < self.risk.min_seconds_to_close:
            raise EngineError("Market is too close to closing")
        if series == "KXBTC15M":
            if (market.get("settlement_bounds_type") != "default"
                    or decimal_value(market.get("notional_value_dollars"), "Bitcoin contract payout") != ONE
                    or market.get("cap_strike") is not None):
                raise EngineError("Bitcoin market requires explicit standard one-dollar settlement")
            if (not isinstance(btc_series, dict) or btc_series.get("ticker") != series
                    or btc_series.get("frequency") != "fifteen_min"):
                raise EngineError("Bitcoin fifteen-minute series identity is unavailable")
            from .btc15m import Btc15mError, fee_reserve_lower_bound
            try:
                minimum_fee_reserve = fee_reserve_lower_bound(btc_series, event)
            except Btc15mError as error:
                raise EngineError(str(error)) from None
            if self.risk.fee_reserve_per_contract < minimum_fee_reserve:
                raise EngineError("Bitcoin fee reserve is below the fresh event fee bound")
            opened = _timestamp(market.get("open_time"))
            closed = _timestamp(market.get("close_time"))
            if closed - opened != 900 or now < opened:
                raise EngineError("Bitcoin market must be an open fifteen-minute window")
            strike = market.get("floor_strike")
            if isinstance(strike, float):
                if not math.isfinite(strike):
                    raise EngineError("Bitcoin market target is unavailable")
                strike = str(strike)
            if (decimal_value(strike, "Bitcoin market target", places=8,
                              maximum=Decimal("100000000")) <= ZERO
                    or market.get("strike_type") not in ("greater", "greater_or_equal")):
                raise EngineError("Bitcoin market requires a posted upward target")
        if not isinstance(raw, dict) or not isinstance(raw.get("orderbook_fp"), dict):
            raise EngineError("Fixed-point order book is unavailable")
        book = raw["orderbook_fp"]
        yes, no = _levels(book.get("yes_dollars")), _levels(book.get("no_dollars"))
        if entered_price is not None and (not yes or not no):
            raise EngineError("A resting limit requires a complete fresh order book")
        if yes and no and yes[-1][0] + no[-1][0] >= ONE:
            raise EngineError("Order book is locked or crossed")
        candidates = []
        for outcome, opposite, probability in (("yes", no, signal["probability_yes"]),
                                               ("no", yes, ONE - signal["probability_yes"])):
            if requested_outcome != 'auto' and outcome != requested_outcome:
                continue
            if (not opposite or (self.risk.sizing_mode == "fixed" and previous and outcome not in previous)
                    or (held_outcome is not None and outcome != held_outcome)):
                continue
            price = entered_price if entered_price is not None else ONE - opposite[-1][0]
            available = opposite[-1][1]
            wire_price = price if outcome == "yes" else ONE - price
            if not _valid_tick(wire_price, market.get("price_ranges")):
                if entered_price is not None:
                    raise EngineError("Entered limit price is outside this market's valid price grid")
                continue
            unit_cost = price + self.risk.fee_reserve_per_contract
            if unit_cost >= ONE:
                continue
            edge = probability - unit_cost
            if edge < self.risk.min_edge:
                continue
            budget = min(self.risk.max_order_cost,
                         self.risk.max_total_cost - self.journal.committed(),
                         self.risk.max_market_cost - self.journal.committed(signal["ticker"]), cash)
            sizing = None
            if self.risk.sizing_mode == "kelly":
                # Replacement-cost-equivalent allocation base. Buying an
                # unchanged target transfers cash into held contracts instead
                # of awarding a fresh full Kelly stake on every new signal.
                # This is not liquidation equity or settled compounding.
                existing_cost = held_quantity * unit_cost
                bankroll = min(selected_bankroll, max(ZERO, cash) + existing_cost)
                full_fraction = max(ZERO, (probability - unit_cost) / (ONE - unit_cost))
                applied_fraction = selected_fraction * full_fraction
                target_cost = bankroll * applied_fraction
                target_contracts = (target_cost / unit_cost).to_integral_value(rounding=ROUND_FLOOR)
                remaining_target_cost = max(ZERO, target_cost - existing_cost)
                budget = min(budget, remaining_target_cost)
                audit = lambda value: format(value.quantize(Decimal("0.000000000001"), rounding=ROUND_FLOOR), "f")
                sizing = dict(mode="kelly", probability=format(probability, "f"),
                    effective_cost=format(unit_cost, "f"), bankroll=format(bankroll, "f"),
                    full_kelly_fraction=audit(full_fraction),
                    kelly_fraction=format(selected_fraction, "f"),
                    selected_bankroll=format(selected_bankroll, "f"),
                    bankroll_ceiling=format(self.risk.kelly_bankroll, "f"),
                    kelly_fraction_ceiling=format(self.risk.kelly_fraction, "f"),
                    applied_fraction=audit(applied_fraction), target_cost=audit(target_cost),
                    budget_cost=audit(max(ZERO, budget)),
                    target_contracts=format(target_contracts, '.0f'),
                    existing_contracts=format(held_quantity, '.0f'),
                    existing_cost=audit(existing_cost),
                    remaining_target_cost=audit(remaining_target_cost))
            caps = [Decimal(self.risk.max_contracts), Decimal(signal["max_count"]),
                    (max(ZERO, budget) / unit_cost).to_integral_value(rounding=ROUND_FLOOR)]
            if entered_price is None:
                caps.append(available.to_integral_value(rounding=ROUND_FLOOR))
            count = min(caps)
            if count >= ONE:
                if sizing is not None:
                    sizing['additional_contracts'] = format(count, '.0f')
                candidates.append((edge, outcome, count, price, wire_price, count * unit_cost, sizing))
        if not candidates:
            if requested_outcome != 'auto':
                raise EngineError(f"Selected {requested_outcome.upper()} contract has no eligible edge, liquidity or remaining risk budget")
            raise EngineError("No eligible edge, liquidity or remaining risk budget")
        _, outcome, count, price, wire_price, cost, sizing = max(candidates, key=lambda item: item[:2])
        return outcome, count, price, wire_price, cost, sizing

    def run_once(self, payload, *, requested_outcome='auto'):
        if not isinstance(requested_outcome, str) or requested_outcome not in ('auto', 'yes', 'no'):
            raise EngineError('Choose automatic, YES or NO contract sizing')
        decisions = []
        with self.journal.exclusive():
            signals = validate_signals(payload, self.risk, self.clock())
            if self.journal.halted():
                return self.journal.export_report([dict(ticker=item["ticker"], status="skipped",
                    reason="Journal has an unresolved order; submissions are halted") for item in signals])
            for signal in signals:
                if not self.journal.consume(signal, self.clock()):
                    decisions.append(dict(ticker=signal["ticker"], status="skipped", reason="Signal was already consumed"))
                    continue
                started = self.clock()
                try:
                    cash = (max(ZERO, self.risk.max_total_cost - self.journal.committed())
                            if self.mode == "paper" else self._account_cash())
                    outcome, count, price, wire_price, cost, sizing = (
                        self._plan(signal, cash, started) if requested_outcome == 'auto'
                        else self._plan(signal, cash, started, requested_outcome=requested_outcome))
                except EngineError as error:
                    decisions.append(dict(ticker=signal["ticker"], status="skipped", reason=str(error)))
                    continue
                except Exception:
                    decisions.append(dict(ticker=signal["ticker"], status="skipped", reason="Market or account data is unavailable"))
                    continue
                # The signal must still be current after all network reads.
                age = self.clock() - _timestamp(payload["generated_at"])
                if age < -5 or age > self.risk.max_signal_age_seconds:
                    decisions.append(dict(ticker=signal["ticker"], status="skipped", reason="Signal expired during data acquisition"))
                    continue
                if sizing is None:
                    client_order_id = self.journal.record(signal, outcome, count, price, cost, self.clock())
                else:
                    client_order_id = self.journal.record(signal, outcome, count, price, cost, self.clock(), sizing)
                # Journal commits can block; freshness must hold immediately before transmission.
                send_time = self.clock()
                send_age = send_time - _timestamp(payload["generated_at"])
                if (send_time < started or send_time - started > self.risk.max_quote_age_seconds
                        or send_age < -5 or send_age > self.risk.max_signal_age_seconds):
                    self.journal.finish(client_order_id, "rejected")
                    decisions.append(dict(ticker=signal["ticker"], status="skipped",
                                          reason="Decision expired during durable journal commit; not submitted"))
                    continue
                if self.mode == "paper":
                    self.journal.finish(client_order_id, "paper_filled", fill_count=count)
                else:
                    wire = dict(ticker=signal["ticker"], client_order_id=client_order_id,
                                side="bid" if outcome == "yes" else "ask",
                                count=format(count, ".2f"), price=format(wire_price, ".4f"),
                                time_in_force="immediate_or_cancel",
                                self_trade_prevention_type="taker_at_cross", cancel_order_on_pause=True)
                    try:
                        response = self.client.create_order(wire)
                        if not isinstance(response, dict):
                            raise EngineError("Invalid order acknowledgement")
                        order_id = response.get("order_id")
                        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 200:
                            raise EngineError("Invalid order acknowledgement")
                        fill = decimal_value(response.get("fill_count"), "filled quantity", places=2, maximum=count)
                        remaining = decimal_value(response.get("remaining_count"), "remaining quantity", places=2, maximum=count)
                        if remaining != ZERO or fill != fill.to_integral_value():
                            raise EngineError("IOC acknowledgement requires reconciliation")
                    except Exception:
                        self.journal.finish(client_order_id, "unknown")
                        decisions.append(dict(ticker=signal["ticker"], status="skipped",
                                              reason="Order outcome is uncertain; submissions are halted"))
                        break
                    self.journal.finish(client_order_id, "filled" if fill > ZERO else "canceled", order_id, fill)
                decision = dict(ticker=signal["ticker"], status="planned", outcome=outcome,
                                count=format(count, ".2f"), price=format(price, ".4f"),
                                reason="Paper top-of-book simulation" if self.mode == "paper" else "IOC order acknowledged")
                if sizing is not None:
                    decision["sizing"] = sizing
                decisions.append(decision)
            return self.journal.export_report(decisions)
