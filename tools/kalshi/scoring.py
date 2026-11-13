"""Pure contract-level probability scoring for labeled shadow forecasts.

Updates from one contract share a single outcome. This module selects the latest
forecast for each contract before scoring; it does not count updates as
independent trials, infer confidence intervals, or estimate trading returns.
"""
from __future__ import annotations

import math
import re


MAX_ROWS = 100_000
MAX_TIMESTAMP = 253_402_300_799
LOG_EPSILON = 1e-15
_TICKER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_REQUIRED = {"ticker", "as_of", "close_time", "probability_yes", "result"}


class ScoringError(ValueError):
    """A fixed validation error that never echoes private input."""


def _number(value, name, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= maximum or not math.isfinite(value)):
        raise ScoringError("Invalid " + name)
    return float(value)


def _loss(probability, outcome):
    # Only logarithms are clipped. Brier scores retain exact 0/1 forecasts.
    clipped = min(1 - LOG_EPSILON, max(LOG_EPSILON, probability))
    return -math.log(clipped) if outcome else -math.log1p(-clipped)


def _metrics(rows, field):
    if not rows:
        return None, None
    return (math.fsum((row[field] - row["outcome"]) ** 2 for row in rows) / len(rows),
            math.fsum(_loss(row[field], row["outcome"]) for row in rows) / len(rows))


def score_forecasts(rows: list[dict]) -> dict:
    """Score the latest pre-close forecast per ticker against official labels.

    Market metrics and paired model metrics use the same subset of contracts
    whose selected forecast contains a market probability. No earlier update is
    substituted when a selected forecast lacks market odds. Callers must supply
    authoritative labels and point-in-time observations; this function cannot
    establish that provenance or validate a training/test split.
    """
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ScoringError("Forecast rows must be a bounded list")
    latest = {}
    identities = {}
    updates = {}
    for raw in rows:
        if not isinstance(raw, dict) or not _REQUIRED <= raw.keys():
            raise ScoringError("Forecast row is incomplete")
        ticker = raw["ticker"]
        if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
            raise ScoringError("Invalid contract ticker")
        result = raw["result"]
        if not isinstance(result, str) or result not in ("yes", "no"):
            raise ScoringError("An official yes or no result is required")
        as_of = _number(raw["as_of"], "forecast timestamp", MAX_TIMESTAMP)
        close = _number(raw["close_time"], "contract close timestamp", MAX_TIMESTAMP)
        if as_of >= close:
            raise ScoringError("Forecast must precede contract close")
        probability = _number(raw["probability_yes"], "forecast probability", 1)
        market = raw.get("market_probability_yes")
        if market is not None:
            market = _number(market, "market probability", 1)
        identity = close, result
        if ticker in identities and identities[ticker] != identity:
            raise ScoringError("Contract close or official result conflicts across updates")
        identities[ticker] = identity
        row = dict(ticker=ticker, as_of=as_of, close_time=close,
                   probability_yes=probability, market_probability_yes=market,
                   outcome=int(result == "yes"))
        key = ticker, as_of
        if key in updates and updates[key] != row:
            raise ScoringError("Conflicting forecasts share a contract and timestamp")
        updates[key] = row
        if ticker not in latest or as_of > latest[ticker]["as_of"]:
            latest[ticker] = row

    selected = [latest[ticker] for ticker in sorted(latest)]
    paired = [row for row in selected if row["market_probability_yes"] is not None]
    brier, log_loss = _metrics(selected, "probability_yes")
    market_brier, market_log_loss = _metrics(paired, "market_probability_yes")
    paired_brier, paired_log_loss = _metrics(paired, "probability_yes")
    bins = []
    for index in range(10):
        members = [row for row in selected
                   if min(9, int(row["probability_yes"] * 10)) == index]
        bins.append(dict(lower=index / 10, upper=(index + 1) / 10,
                         count=len(members),
                         mean_probability=(math.fsum(row["probability_yes"] for row in members)
                                           / len(members) if members else None),
                         observed_frequency=(math.fsum(row["outcome"] for row in members)
                                             / len(members) if members else None)))
    return dict(kind="kalshi_shadow_scores", version=1,
                selection="latest_forecast_per_contract", count_contracts=len(selected),
                count_paired_contracts=len(paired), brier_score=brier, log_loss=log_loss,
                market_brier_score=market_brier, market_log_loss=market_log_loss,
                paired_model_brier_score=paired_brier, paired_model_log_loss=paired_log_loss,
                mean_leadtime_seconds=(math.fsum(row["close_time"] - row["as_of"]
                                                for row in selected) / len(selected)
                                       if selected else None),
                reliability_bins=bins)
