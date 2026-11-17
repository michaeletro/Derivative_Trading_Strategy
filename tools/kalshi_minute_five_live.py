#!/usr/bin/env python3
"""Account preflight, or explicitly armed minute-five fixed-budget real entries.

Without --run --allow-live this command cannot submit orders. By default the
journal keeps a lifetime spending ceiling; --continuous reuses allowance only
after confirmed final settlement, retaining all historical orders.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.account_reads import ParallelAccountReader
from kalshi.discovery_cache import LocalDiscoveryCache
from kalshi.report_publisher import CoalescingReportPublisher, ReportPublicationError
from kalshi.report_snapshots import JournalReportCache, PreparedReport, PairedReportWriter, status_report_path
from kalshi.background_settlement import BackgroundSettlement
from kalshi.execution_assets import execution_asset_spec as asset_spec
from kalshi.engine import Bot, EngineError, Journal, RiskConfig, decimal_value
from kalshi.entry_schedule import EntrySchedule, EntryScheduleError
from kalshi.fitted_parameters import FittedParameterError, load_fitted_parameters
from kalshi.forecast import ForecastConfig
from kalshi.calibrated_model import CalibrationModelError, load_calibration_model
from kalshi.minute_five import (MinuteFiveError, MinuteFiveObservationService,
                               minute_five_forecast_config, minute_five_model_definition)
from kalshi.minute_five_execution import MinuteFiveLiveExecutor
from kalshi.minute_five_policy import (budget_text, execution_policy, parse_bet_budget, parse_exit_bid_threshold,
                                       parse_exit_window_seconds, parse_opposite_after_seconds,
                                       parse_probability_ceiling, recurring_budget_plan,
                                       update_bet_budget, update_cash_allocation,
                                       update_entry_schedule, update_risk_limits)
from kalshi.shadow import ShadowArchive, ShadowError
from kalshi.settlement_allowance import refresh_settled_allowances
from kalshi.settled_exposure import verify_settled_exposure
from kalshi.time_sizing import GaussianSizing, UniformSizing, TimeSizingError
from kalshi.uniform_pacing import UniformPacingConfig, UniformPacingError
from kalshi.regime import RegimeConfig, RegimeError
from kalshi.rolling_kelly import RollingKellyConfig
from kalshi.pyth_stream import KalshiPythStream
from kalshi.gold_live import GoldObservationService, gold_model_definition
from kalshi.parallel_live import ParallelObservationPool, ParallelCoordinatorLease
from kalshi.scheduled_policy import ScheduledPolicyConfig
from kalshi.scheduled_activity import ScheduledActivityAudit, capture_scheduled_journal
from kalshi.research_capture import (ResearchObserver, ResearchExecutor, source_fingerprints,
    research_health, _emit as emit_research)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--journal', type=Path,
        default=Path.home() / '.local/state/dts-kalshi/minute-five-live-v1.sqlite3')
    result.add_argument('--max-total-cost', required=True,
        help='Lifetime dollar ceiling, or active reserved-cost ceiling with --continuous; saved changes require an explicit update')
    result.add_argument('--continuous', action='store_true',
        help='Reuse global and cash allowance after confirmed settlement; requires every-minute and an explicit schedule update to switch existing journals')
    result.add_argument('--continue-unresolved', action='store_true',
        help='With --continuous, quarantine markets with fully reserved uncertain IOC attempts and continue other markets; an existing journal requires a schedule update')
    result.add_argument('--skip-probability-at-or-above',
        help='Skip recurring entries whose selected-side Monte Carlo probability is at or above this fraction, for example 0.95')
    result.add_argument('--exit-bid-above',
        help='While the contract is open, close journal-owned holdings when their executable bid is strictly above this dollar price; requires every-minute and an explicit schedule update')
    result.add_argument('--exit-seconds-before-close', type=int,
        help='Price-exit window before expiry, from 1 through 900 seconds; default 900 covers the full 15-minute contract; 60 restores final-minute exits')
    result.add_argument('--stop-bid-at-or-below',
        help='Close journal-owned holdings when their executable bid is at or below this dollar price in the late stop window; requires recurring entries and an explicit schedule update')
    result.add_argument('--stop-seconds-before-close', type=int,
        help='Arm the stop only with strictly fewer than this many seconds to expiry, from 1 through 900; default 300 means less than five minutes')
    result.add_argument('--opposite-bid-at-or-below',
        help='After the elapsed-time delay, target opposite scheduled purchases when the held-side executable bid is at or below this dollar price; requires recurring entries and an explicit schedule update')
    result.add_argument('--opposite-after-seconds', type=int,
        help='Earliest elapsed market second for opposite-side targeting, from 0 through 899; default 450 means after seven and a half minutes')
    result.add_argument('--late-model-exit-probability-above',
        help='In the late model window, close an opposing holding when the selected model direction probability is strictly above this fraction; defaults to first-fresh direction selection; requires recurring entries and a schedule update')
    result.add_argument('--late-model-exit-seconds-before-close', type=int,
        help='Arm the late model exit when this many seconds or fewer remain, from 1 through 900; default 180 means three minutes remaining')
    result.add_argument('--eth-late-model-direction', choices=('first-fresh', 'latest-fresh'), default='first-fresh',
        help='ETH only: retain the first fresh direction (default), or select the latest fresh majority before an exit triggers; latest-fresh requires ETH, an enabled late model exit and an explicit schedule update for existing journals')
    result.add_argument('--resume-after-exit', action='store_true',
        help='After a confirmed full early price exit, resume on the next scheduled entry window only while more than 60 seconds remain; requires an enabled bid exit and explicit schedule update')
    result.add_argument('--regime-switching', action='store_true',
        help='Opt into the experimental BTC, ETH, SOL or XRP regime policy: freeze each contract regime before first entry, use normal model choices in trends, and close then switch range targets at 50 cents; requires recurring entries, a bid exit and resumption')
    result.add_argument('--regime-half-life-seconds', type=float,
        help='Experimental regime EWMA half-life in elapsed seconds; default 300; requires --regime-switching')
    result.add_argument('--regime-lookback-seconds', type=int,
        help='Each asset history used to classify its contract before first entry; default 1800; requires --regime-switching and enough model archive history')
    result.add_argument('--regime-transition', choices=('wait', 'model'),
        help='For a valid transition regime, wait (default) or use normal Monte Carlo side selection; unavailable evidence remains blocked; requires --regime-switching and a schedule update')
    result.add_argument('--regime-range-rule', choices=('filtered', 'between-trends'),
        help='Classify ranges with the existing filtered rule (default), or every valid score strictly between the trend thresholds; requires --regime-switching and a schedule update')
    result.add_argument('--sizing-profile', choices=('fixed', 'gaussian', 'uniform'), default='fixed',
        help='Fixed phased stakes, a Gaussian time profile, or equal uniform allowances across the full market')
    result.add_argument('--gaussian-budget',
        help='Total risk cap per market for Gaussian sizing; default 33 dollars')
    result.add_argument('--gaussian-peak-seconds', type=float,
        help='Gaussian peak in elapsed market seconds; default 450')
    result.add_argument('--gaussian-width-seconds', type=float,
        help='Gaussian standard deviation in seconds; default 180')
    result.add_argument('--uniform-budget',
        help='Total risk cap per market split equally across entry windows with uniform sizing; default 33 dollars')
    result.add_argument('--entry-pacing', choices=('slots', 'cumulative'), default='slots',
        help='Keep isolated uniform slot allowances, or carry unused released allowance forward within a bounded order cap')
    result.add_argument('--pacing-carry-slots', type=int,
        help='Maximum cumulative order allowance in uniform slices; default 3; requires --entry-pacing cumulative')
    result.add_argument('--rolling-kelly', action='store_true',
        help='Opt into BTC, ETH, SOL and XRP rolling aggregate Kelly targets, two-sided fee-adjusted entries and model-relative exits; requires recurring uniform sizing and shared account limits')
    result.add_argument('--kelly-fraction',
        help='Fraction of the aggregate full Kelly exposure target, from zero exclusive through one; default 0.25')
    result.add_argument('--kelly-min-entry-edge',
        help='Minimum conservative fee-adjusted entry edge in payout dollars per contract; default 0.01')
    result.add_argument('--kelly-probability-margin',
        help='Absolute probability sensitivity haircut; default 0.02. This is a configured assumption, not a calibrated confidence bound')
    result.add_argument('--kelly-exit-edge',
        help='Minimum net sale proceeds above conservative holding value for a model-value exit; default 0.01')
    result.add_argument('--kelly-slippage',
        help='Additional execution-cost allowance per contract for rolling Kelly; default 0.001')
    result.add_argument('--kelly-cooldown-seconds', type=int,
        help='Minimum delay after a rolling model exit before renewed entry; default 8 seconds')
    result.add_argument('--kelly-reentry-edge-increment',
        help='Additional entry edge required after a model exit; default 0.005')
    result.add_argument('--show-sizing-plan', action='store_true',
        help='Print the allocation offline without credentials, journal changes or orders')
    result.add_argument('--bet-budget', default='1',
        help='Maximum dollars per regular entry including fees; defaults to 1, with an optional initial-entry override')
    result.add_argument('--initial-bet-budget',
        help='Optional fee-inclusive budget for the first main-phase entry; later main entries use --bet-budget')
    result.add_argument('--early-bet-budget',
        help='Optional fee-inclusive budget for one entry per minute before the main warmup ends; requires a ready Monte Carlo model')
    result.add_argument('--cash-allocation',
        help='Lifetime cash allowance, or active working cash allowance with --continuous; requires --update-cash-allocation')
    result.add_argument('--update-cash-allocation', action='store_true',
        help='Explicitly update the saved cash allowance without changing order history or risk limits; stop the old runner first')
    result.add_argument('--entry-schedule', choices=('once', 'every-minute'), default='once',
        help='once: one minute-five entry; every-minute: recurring fixed-budget entries at the configured warmup and cadence')
    result.add_argument('--warmup-seconds', type=int,
        help='Entry delay: default 300 for fixed sizing, 0 for Gaussian or uniform; independent of model history')
    result.add_argument('--bet-every-seconds', type=int, default=60,
        help='Seconds between recurring entry windows, anchored to market opening plus warmup')
    result.add_argument('--history-seconds', type=int,
        help='Trailing live model lookback, 150 through 3600 seconds; defaults to 1800 or the loaded fit\'s window')
    result.add_argument('--min-history-seconds', type=int,
        help='Minimum live history before forecasting; a loaded fit retains its longer required minimum')
    result.add_argument('--update-entry-schedule', action='store_true',
        help='Apply the recurring schedule, entry budgets and cumulative ceiling while preserving prior spending and orders; stop the old runner first')
    result.add_argument('--add-assets', action='store_true',
        help='With --update-entry-schedule, explicitly expand the saved asset scope while preserving all prior orders and shared limits')
    result.add_argument('--check-gold-feed', action='store_true',
        help='Check authenticated live Gold Pyth access without placing orders unless also armed; never activates a subscription')
    result.add_argument('--gold-feed-wait-seconds', type=float, default=3,
        help='Maximum wait for an actual fresh Gold tick during --check-gold-feed, from 0 through 30 seconds')
    result.add_argument('--update-risk-limits', action='store_true',
        help='Update an existing every-minute journal\'s cumulative ceiling and contract cap without resetting prior spending or orders; stop the old runner first')
    result.add_argument('--update-bet-budget', action='store_true',
        help='Update this journal\'s per-bet and quantity ceilings without resetting orders or changing its cumulative cap; stop the old runner first')
    result.add_argument('--run', action='store_true', help='Continuously submit eligible real orders')
    result.add_argument('--allow-live', action='store_true', help='Authorize real production orders; requires --run')
    result.add_argument('--asset', action='append', choices=('BTC', 'ETH', 'SOL', 'XRP', 'GOLD'))
    result.add_argument('--trade-asset', action='append', choices=('BTC', 'ETH', 'SOL', 'XRP'),
        help='Restrict new entries to this subset of --asset for this run; repeat as needed. All managed assets retain their journal history and protective exits')
    result.add_argument('--entry-contracts', type=int,
        help='Request this exact whole-contract quantity on eligible entries, or skip when it cannot fit; IOC fills can be partial. Protective exit sizing is unchanged')
    result.add_argument('--account-read-mode', choices=('parallel', 'sequential'), default='parallel',
        help='Live account reads: overlap four paced read-only endpoints by default; sequential is an explicit comparison/fallback mode')
    result.add_argument('--settlement-mode', choices=('background', 'sequential'), default='background',
        help='Gather periodic settlement evidence on a read-only worker; the financial writer validates and applies it')
    result.add_argument('--planning-read-mode', choices=('parallel', 'sequential'), default='parallel',
        help='Overlap independent cold planning metadata reads through paced read-only transports; sequential is a comparison/fallback mode')
    result.add_argument('--report-mode', choices=('coalesced', 'sequential'), default='coalesced',
        help='Publish routine report files with a bounded latest-only writer; halt and unknown-order reports remain synchronous')
    result.add_argument('--parallel-assets', action='store_true',
        help='Run independent read-only crypto model workers with one parent owning all trading and shared account limits; requires at least two assets and recurring uniform sizing')
    result.add_argument('--worker-asset', choices=('BTC', 'ETH', 'SOL', 'XRP'), help=argparse.SUPPRESS)
    result.add_argument('--observation-worker', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--observation-session', help=argparse.SUPPRESS)
    result.add_argument('--decision-model', choices=('monte-carlo', 'analytic', 'calibrated'), default='monte-carlo',
        help='Use Monte Carlo, analytic Gaussian probability, or a fitted calibration of its exact bound base model')
    result.add_argument('--probability-calibration', action='append', default=[], metavar='ASSET=PATH',
        help='Asset-bound fitted calibration artifact: recorded v1 or historical-population v2; requires --decision-model calibrated')
    result.add_argument('--eth-garch-model', type=Path,
        help='Explicitly override only ETH probabilities with a qualified frozen Gaussian GARCH research-model.json; other assets keep --decision-model; requires recurring ETH entries and its own completed-minute history')
    result.add_argument('--scheduled-entry-edge',
        help='Optional minimum fee-adjusted executable entry edge for the scheduled uniform strategy; omitted preserves original direction selection')
    result.add_argument('--scheduled-probability-margin',
        help='Optional absolute probability sensitivity allowance for scheduled entry edges; default zero, not a calibrated confidence bound')
    result.add_argument('--entry-slippage',
        help='Additional per-contract cost allowance for scheduled entry edges; default zero')
    result.add_argument('--min-exit-cooldown-seconds', type=int,
        help='Optional minimum delay after a confirmed full exit before scheduled re-entry; default zero')
    result.add_argument('--reentry-probability-move',
        help='Optional minimum absolute YES-probability change after a confirmed full exit before re-entry; default zero')
    result.add_argument('--activity-report', type=Path,
        help='Optional separate report of actual venue fills, fees and reconciled trading activity')
    result.add_argument('--research-dir', type=Path,
        help='Append request attempts, observations, every decision and actual-fill audits to separate private research shards')
    result.add_argument('--research-run-id', help=argparse.SUPPRESS)
    result.add_argument('--read-min-interval', type=float, default=.05,
        help='Parallel-mode aggregate GET admission spacing in seconds (.02 through 1); local cap, not a venue-tier guarantee')
    result.add_argument('--audit-interval-seconds', type=float, default=30,
        help='Seconds between bounded read-only actual-fill audits; default 30, from one through 3600')
    result.add_argument('--paths', type=int, default=4000)
    result.add_argument('--probability-fit', type=Path)
    result.add_argument('--asset-probability-fit', action='append', default=[], metavar='ASSET=PATH',
        help='Asset-bound probability fit; repeat for separate BTC/ETH/SOL/XRP artifacts. Unspecified assets use their own baseline')
    result.add_argument('--fee-reserve-per-contract', default='0.03')
    result.add_argument('--max-contracts', type=int,
        help='Optional quantity cap; default is 5 for once-only one-dollar entries, otherwise derived from the larger entry budget and fee reserve')
    result.add_argument('--interval', type=float, default=3)
    result.add_argument('--timeout', type=float, default=1)
    result.add_argument('--report', type=Path, help='Optional atomic live execution report; separate from paper reports')
    result.add_argument('--stop-file', type=Path, help='Its existence stops new orders; held positions remain open')
    return result


def _gaussian_profile(args):
    supplied = (args.gaussian_budget, args.gaussian_peak_seconds, args.gaussian_width_seconds)
    if args.sizing_profile != 'gaussian':
        if any(value is not None for value in supplied):
            raise CLIError('Gaussian parameters require --sizing-profile gaussian')
        return None
    if (args.entry_schedule != 'every-minute' or args.initial_bet_budget is not None
            or args.early_bet_budget is not None or parse_bet_budget(args.bet_budget) != 1):
        raise CLIError('Gaussian sizing requires recurring entries and replaces fixed initial, early and per-bet budgets')
    return GaussianSizing(budget='33' if args.gaussian_budget is None else args.gaussian_budget,
        peak_seconds=450 if args.gaussian_peak_seconds is None else args.gaussian_peak_seconds,
        width_seconds=180 if args.gaussian_width_seconds is None else args.gaussian_width_seconds)


def _time_sizing_profile(args):
    gaussian = _gaussian_profile(args)
    if args.sizing_profile != 'uniform':
        if args.uniform_budget is not None:
            raise CLIError('A uniform budget requires --sizing-profile uniform')
        return gaussian
    if (args.entry_schedule != 'every-minute' or args.initial_bet_budget is not None
            or args.early_bet_budget is not None or parse_bet_budget(args.bet_budget) != 1):
        raise CLIError('Uniform sizing requires recurring entries and replaces fixed initial, early and per-bet budgets')
    return UniformSizing(budget='33' if args.uniform_budget is None else args.uniform_budget)


def _time_sizing_settings(profile):
    if isinstance(profile, UniformSizing):
        return dict(uniform_sizing=profile)
    return {} if profile is None else dict(gaussian_sizing=profile)


def _uniform_pacing_configuration(args):
    mode = getattr(args, 'entry_pacing', 'slots')
    slices = getattr(args, 'pacing_carry_slots', None)
    if mode == 'slots':
        if slices is not None:
            raise CLIError('A pacing carry cap requires --entry-pacing cumulative')
        return None
    selected = tuple(args.asset or ('BTC',))
    if (mode != 'cumulative' or args.sizing_profile != 'uniform'
            or args.entry_schedule != 'every-minute' or args.warmup_seconds not in (None, 0)
            or not selected or not set(selected) <= {'BTC', 'ETH', 'SOL', 'XRP'}
            or args.rolling_kelly or args.regime_switching or args.opposite_bid_at_or_below is not None):
        raise CLIError('Cumulative pacing requires recurring crypto uniform sizing without Kelly, regime or opposite-side overlays')
    try:
        return UniformPacingConfig(carry_slots=3 if slices is None else slices)
    except UniformPacingError as error:
        raise CLIError(str(error)) from None


def _regime_configuration(args):
    supplied = (args.regime_half_life_seconds, args.regime_lookback_seconds,
        args.regime_transition, args.regime_range_rule)
    if not args.regime_switching:
        if any(value is not None for value in supplied):
            raise CLIError('Regime parameters require --regime-switching')
        return None
    selected = tuple(args.asset or ('BTC',))
    if (not selected or len(selected) != len(set(selected))
            or not set(selected) <= {'BTC', 'ETH', 'SOL', 'XRP'}):
        raise CLIError('Regime switching requires distinct BTC, ETH, SOL or XRP assets')
    if (args.entry_schedule != 'every-minute' or args.exit_bid_above is None
            or not args.resume_after_exit):
        raise CLIError('Regime switching requires recurring entries, --exit-bid-above and --resume-after-exit')
    settings = {}
    if args.regime_half_life_seconds is not None:
        settings['half_life_seconds'] = args.regime_half_life_seconds
    if args.regime_lookback_seconds is not None:
        settings.update(lookback_seconds=args.regime_lookback_seconds,
            min_history_seconds=args.regime_lookback_seconds - 5)
    if args.regime_transition == 'model':
        settings['transition_action'] = 'model'
    if args.regime_range_rule == 'between-trends':
        settings['range_rule'] = 'between_trends'
    return RegimeConfig(**settings)


def _rolling_kelly_configuration(args):
    fields = dict(fraction=args.kelly_fraction, min_entry_edge=args.kelly_min_entry_edge,
        probability_margin=args.kelly_probability_margin, exit_edge=args.kelly_exit_edge,
        slippage_per_contract=args.kelly_slippage, cooldown_seconds=args.kelly_cooldown_seconds,
        reentry_edge_increment=args.kelly_reentry_edge_increment)
    if not args.rolling_kelly:
        if any(value is not None for value in fields.values()):
            raise CLIError('Kelly parameters require --rolling-kelly')
        return None
    selected = tuple(args.asset or ('BTC',))
    if (not selected or len(selected) != len(set(selected))
            or not set(selected) <= {'BTC', 'ETH', 'SOL', 'XRP'} or args.entry_schedule != 'every-minute'
            or args.sizing_profile != 'uniform' or args.warmup_seconds not in (None, 0)):
        raise CLIError('Rolling Kelly requires distinct BTC, ETH, SOL or XRP assets with recurring uniform sizing and zero warmup')
    if (args.skip_probability_at_or_above is not None or args.exit_bid_above is not None
            or args.exit_seconds_before_close is not None or args.regime_switching
            or args.opposite_bid_at_or_below is not None or args.opposite_after_seconds is not None
            or args.late_model_exit_probability_above is not None
            or args.late_model_exit_seconds_before_close is not None or args.resume_after_exit):
        raise CLIError('Rolling Kelly replaces legacy probability, profit, direction and re-entry overlays; omit those flags')
    return RollingKellyConfig(**{name: value for name, value in fields.items() if value is not None})


def _parallel_configuration(args):
    selected = tuple(args.asset or ('BTC',))
    if args.worker_asset is not None and not args.observation_worker:
        raise CLIError('Asset workers are read-only; --worker-asset requires --observation-worker')
    if (args.observation_worker or args.observation_session is not None) and not args.parallel_assets:
        raise CLIError('An observation worker requires --parallel-assets')
    if not args.parallel_assets:
        return
    if (not 2 <= len(selected) <= 4 or len(set(selected)) != len(selected)
            or not set(selected) <= {'BTC', 'ETH', 'SOL', 'XRP'}
            or args.entry_schedule != 'every-minute' or args.sizing_profile != 'uniform'
            or args.rolling_kelly or args.regime_switching):
        raise CLIError('Parallel assets require two through four distinct crypto assets with recurring uniform sizing and no rolling Kelly or regime overlay')
    if args.observation_worker:
        if args.worker_asset not in selected or args.run or args.allow_live:
            raise CLIError('An observation worker must select one asset and cannot enable live writes')
        session = args.observation_session
        if (not isinstance(session, str) or len(session) != 32
                or any(character not in '0123456789abcdef' for character in session)):
            raise CLIError('An observation worker requires its supervisor session identity')
        if args.report is None or args.activity_report is not None or args.show_sizing_plan:
            raise CLIError('An observation worker requires its own report and cannot access activity history')
        if (args.update_entry_schedule or args.update_risk_limits or args.update_bet_budget
                or args.update_cash_allocation or args.cash_allocation is not None or args.add_assets
                or args.trade_asset is not None or args.entry_contracts is not None
                or getattr(args, 'eth_late_model_direction', 'first-fresh') != 'first-fresh'):
            raise CLIError('Only the trading parent may update journal policy or cash allocation')
    elif args.observation_session is not None:
        raise CLIError('An observation session is private to a read-only worker')


def _scheduled_configuration(args):
    values = dict(min_entry_edge=args.scheduled_entry_edge,
        probability_margin=args.scheduled_probability_margin, slippage=args.entry_slippage,
        min_exit_cooldown_seconds=args.min_exit_cooldown_seconds,
        reentry_probability_move=args.reentry_probability_move)
    if all(value is None for value in values.values()):
        return None
    if (args.entry_schedule != 'every-minute' or args.sizing_profile != 'uniform'
            or args.rolling_kelly or args.regime_switching or args.opposite_bid_at_or_below is not None
            or 'GOLD' in (args.asset or ())):
        raise CLIError('Scheduled edge and re-entry policies require crypto recurring uniform sizing without Kelly, regime or opposite overlays')
    return ScheduledPolicyConfig(**{name: value for name, value in values.items() if value is not None})


def _entry_controls(args, assets, risk):
    """Runtime restrictions on new entries; retain the complete journal scope."""
    selected = getattr(args, 'trade_asset', None)
    count = getattr(args, 'entry_contracts', None)
    if selected is None and count is None:
        return {}
    if (args.entry_schedule != 'every-minute' or args.sizing_profile != 'uniform'
            or not set(assets) <= {'BTC', 'ETH', 'SOL', 'XRP'}
            or args.rolling_kelly or args.regime_switching
            or args.opposite_bid_at_or_below is not None):
        raise CLIError('Entry asset and quantity controls require recurring crypto uniform sizing without Kelly, regime or opposite overlays')
    result = {}
    if selected is not None:
        if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(assets):
            raise CLIError('Choose each --trade-asset once from the managed --asset scope')
        result['trade_assets'] = tuple(sorted(selected))
    if count is not None:
        if type(count) is not int or not 1 <= count <= min(10000, risk.max_contracts):
            raise CLIError('Entry contracts must be a whole number from one through the risk contract cap, at most 10000')
        result['entry_contracts'] = count
    return result


def _decision_settings(args, asset=None):
    garch_model = getattr(args, '_eth_garch_model', None)
    if asset == 'ETH' and garch_model is not None:
        return dict(decision_model='garch', garch_model=garch_model)
    if args.decision_model == 'monte-carlo':
        return {}
    settings = dict(decision_model=args.decision_model)
    if args.decision_model == 'calibrated':
        model = getattr(args, '_probability_calibrations', {}).get(asset)
        if model is None:
            raise CLIError('A fitted probability calibration is required for ' + str(asset))
        settings['probability_calibration'] = model
    return settings


def _garch_settings(args, asset):
    model = getattr(args, '_eth_garch_model', None)
    return {} if asset != 'ETH' or model is None else dict(garch_model=model)


def _garch_report_fields(args, assets, *, sizing_only=False):
    if getattr(args, 'eth_garch_model', None) is None:
        return {}
    result = dict(asset_decision_models={asset: 'garch' if asset == 'ETH' else args.decision_model
        for asset in assets}, eth_garch_model_status=(
            'not_loaded_sizing_only' if sizing_only else 'historically_fitted_not_live_validated'),
        garch_minimum_completed_minute_returns=120)
    model = getattr(args, '_eth_garch_model', None)
    if model is not None:
        result['eth_garch_model'] = model.metadata()
    return result


def load_live_garch_model(path, *, paths, seed):
    # Keep the extra model family out of legacy startup and its import graph.
    from kalshi.garch_live import load_live_garch_model as load_model
    try:
        return load_model(path, paths=paths, seed=seed)
    except (ValueError, TypeError, KeyError, OSError):
        raise CLIError('The selected ETH GARCH artifact is unavailable or incompatible') from None


def _calibration_bindings(args, assets):
    values = getattr(args, 'probability_calibration', ())
    garch_enabled = getattr(args, 'eth_garch_model', None) is not None
    if garch_enabled and any(value.partition('=')[0] == 'ETH' for value in values):
        raise CLIError('Remove the ETH --probability-calibration binding when --eth-garch-model is selected')
    if args.decision_model != 'calibrated':
        if values:
            raise CLIError('Probability calibration artifacts require --decision-model calibrated')
        return {}
    if 'GOLD' in assets:
        raise CLIError('The calibrated decision model supports BTC, ETH, SOL and XRP')
    expected = {args.worker_asset} if args.observation_worker else set(assets)
    if garch_enabled:
        expected.discard('ETH')
    result = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in expected or asset in result or not path:
            raise CLIError('Provide one --probability-calibration ASSET=PATH for each selected worker asset')
        result[asset] = _private_path(Path(path))
    if not args.show_sizing_plan and set(result) != expected:
        missing = ', '.join(sorted(expected - set(result)))
        raise CLIError('The calibrated model requires fitted artifacts for: ' + missing)
    return result


def _configuration(args):
    garch_path = getattr(args, 'eth_garch_model', None)
    args._eth_garch_model, args._eth_garch_model_path = None, None
    if garch_path is not None:
        if 'ETH' not in (args.asset or ('BTC',)) or args.entry_schedule != 'every-minute':
            raise CLIError('--eth-garch-model requires managed ETH and recurring entries')
        if args.observation_worker and args.worker_asset != 'ETH':
            raise CLIError('Only the ETH observation worker may load --eth-garch-model')
        if any(value.partition('=')[0] == 'ETH' for value in args.asset_probability_fit):
            raise CLIError('An ETH GARCH model cannot use an ETH legacy probability fit')
        args._eth_garch_model_path = _private_path(garch_path)
    eth_direction = getattr(args, 'eth_late_model_direction', 'first-fresh')
    if eth_direction not in ('first-fresh', 'latest-fresh'):
        raise CLIError('Choose first-fresh or latest-fresh for the ETH late model direction')
    if eth_direction == 'latest-fresh' and ('ETH' not in (args.asset or ('BTC',))
            or args.entry_schedule != 'every-minute' or args.late_model_exit_probability_above is None):
        raise CLIError('Latest-fresh ETH direction requires --asset ETH, recurring entries and an enabled late model exit')
    if (type(args.read_min_interval) not in (int, float) or not math.isfinite(args.read_min_interval)
            or not .02 <= args.read_min_interval <= 1):
        raise CLIError('Shared read admission spacing must be .02 through one second')
    _rolling_kelly_configuration(args)
    _parallel_configuration(args)
    _scheduled_configuration(args)
    if (not math.isfinite(args.audit_interval_seconds) or not 1 <= args.audit_interval_seconds <= 3600):
        raise CLIError('The actual-fill audit interval must be from one through 3600 seconds')
    if args.decision_model in ('analytic', 'calibrated') and 'GOLD' in (args.asset or ()):
        raise CLIError('Analytic and calibrated decision models require supported crypto settlement contracts')
    if args.add_assets and not args.update_entry_schedule:
        raise CLIError('Adding assets requires --update-entry-schedule')
    if 'GOLD' in (args.asset or ()) and args.entry_schedule != 'every-minute':
        raise CLIError('Gold live entries require --entry-schedule every-minute')
    if args.check_gold_feed and 'GOLD' not in (args.asset or ()):
        raise CLIError('A Gold feed check requires --asset GOLD')
    if not math.isfinite(args.gold_feed_wait_seconds) or not 0 <= args.gold_feed_wait_seconds <= 30:
        raise CLIError('The Gold feed wait must be from zero through thirty seconds')
    if args.skip_probability_at_or_above is not None:
        if args.entry_schedule != 'every-minute':
            raise CLIError('An entry probability ceiling requires --entry-schedule every-minute')
        parse_probability_ceiling(args.skip_probability_at_or_above)
    if args.exit_bid_above is not None:
        if args.entry_schedule != 'every-minute':
            raise CLIError('A price exit requires --entry-schedule every-minute')
        parse_exit_bid_threshold(args.exit_bid_above)
        args.exit_seconds_before_close = parse_exit_window_seconds(
            900 if args.exit_seconds_before_close is None else args.exit_seconds_before_close)
    elif args.exit_seconds_before_close is not None:
        raise CLIError('An exit window requires --exit-bid-above')
    if args.stop_bid_at_or_below is not None:
        if args.entry_schedule != 'every-minute':
            raise CLIError('A late bid stop requires --entry-schedule every-minute')
        stop_threshold = parse_exit_bid_threshold(args.stop_bid_at_or_below)
        args.stop_seconds_before_close = parse_exit_window_seconds(
            300 if args.stop_seconds_before_close is None else args.stop_seconds_before_close)
        if (args.exit_bid_above is not None
                and stop_threshold >= parse_exit_bid_threshold(args.exit_bid_above)):
            raise CLIError('The stop bid threshold must be below the profit exit bid threshold')
    elif args.stop_seconds_before_close is not None:
        raise CLIError('A stop window requires --stop-bid-at-or-below')
    if args.opposite_bid_at_or_below is not None:
        if args.entry_schedule != 'every-minute':
            raise CLIError('Opposite-side purchases require --entry-schedule every-minute')
        opposite_threshold = parse_exit_bid_threshold(args.opposite_bid_at_or_below)
        args.opposite_after_seconds = parse_opposite_after_seconds(
            450 if args.opposite_after_seconds is None else args.opposite_after_seconds)
        if (args.stop_bid_at_or_below is not None
                and parse_exit_bid_threshold(args.stop_bid_at_or_below) >= opposite_threshold):
            raise CLIError('The stop bid threshold must be below the opposite-side threshold')
        if (args.exit_bid_above is not None
                and opposite_threshold >= parse_exit_bid_threshold(args.exit_bid_above)):
            raise CLIError('The opposite-side threshold must be below the profit exit bid threshold')
    elif args.opposite_after_seconds is not None:
        raise CLIError('An opposite-side delay requires --opposite-bid-at-or-below')
    if args.late_model_exit_probability_above is not None:
        if args.entry_schedule != 'every-minute':
            raise CLIError('A late model-direction exit requires --entry-schedule every-minute')
        parse_exit_bid_threshold(args.late_model_exit_probability_above)
        args.late_model_exit_seconds_before_close = parse_exit_window_seconds(
            180 if args.late_model_exit_seconds_before_close is None
            else args.late_model_exit_seconds_before_close)
    elif args.late_model_exit_seconds_before_close is not None:
        raise CLIError('A late model exit window requires --late-model-exit-probability-above')
    if args.resume_after_exit and (args.exit_bid_above is None or args.entry_schedule != 'every-minute'):
        raise CLIError('Resuming after an exit requires --exit-bid-above and --entry-schedule every-minute')
    regime_config = _regime_configuration(args)
    if args.continue_unresolved and not args.continuous:
        raise CLIError('Continuing past unresolved IOC attempts requires --continuous')
    if args.continuous and args.entry_schedule != 'every-minute':
        raise CLIError('Continuous trading requires --entry-schedule every-minute')
    profile = _time_sizing_profile(args)
    args._uniform_pacing_config = _uniform_pacing_configuration(args)
    if args.warmup_seconds is None:
        args.warmup_seconds = 0 if profile is not None else 300
    schedule = EntrySchedule(args.warmup_seconds, args.bet_every_seconds,
        early_entries=args.early_bet_budget is not None)
    if profile is not None and args.warmup_seconds != 0:
        raise CLIError(f'{args.sizing_profile.capitalize()} sizing covers the full contract; use --warmup-seconds 0')
    if args.early_bet_budget is not None and args.entry_schedule != 'every-minute':
        raise CLIError('Early entry budgets require --entry-schedule every-minute')
    if args.entry_schedule == 'once' and schedule != EntrySchedule():
        raise CLIError('Custom warmup and betting cadence require --entry-schedule every-minute')
    if (sum((args.update_entry_schedule, args.update_bet_budget, args.update_risk_limits)) > 1
            or args.update_cash_allocation and args.update_bet_budget):
        raise CLIError('Choose one policy update; cash allocation may accompany an entry-schedule or risk-limit update')
    if args.show_sizing_plan and any((args.run, args.allow_live, args.update_entry_schedule,
            args.update_bet_budget, args.update_risk_limits, args.update_cash_allocation, args.check_gold_feed)):
        raise CLIError('An offline sizing plan cannot submit orders or apply journal updates')
    if args.update_cash_allocation != (args.cash_allocation is not None):
        raise CLIError('Use --cash-allocation and --update-cash-allocation together')
    if args.cash_allocation is not None:
        parse_bet_budget(args.cash_allocation)
    if args.update_entry_schedule and args.entry_schedule != 'every-minute':
        raise CLIError('An entry-schedule update requires --entry-schedule every-minute')
    if args.update_bet_budget and args.entry_schedule != 'once':
        raise CLIError('Use --update-entry-schedule for the every-minute strategy')
    if args.update_risk_limits and args.entry_schedule != 'every-minute':
        raise CLIError('A risk-limit update requires --entry-schedule every-minute')
    if args.initial_bet_budget is not None and args.entry_schedule != 'every-minute':
        raise CLIError('An initial entry budget requires --entry-schedule every-minute')
    if args.run != args.allow_live:
        raise CLIError('Real execution requires both --run and --allow-live; omit both for read-only preflight')
    if (not math.isfinite(args.interval) or not 1 <= args.interval <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3):
        raise CLIError('Interval must be 1 through 60 seconds; transport timeout must be positive and at most 3 seconds')
    total = decimal_value(args.max_total_cost, 'total spending ceiling', maximum=Decimal('1000000'))
    if total <= 0:
        raise CLIError('The total spending ceiling must be positive')
    bet_budget = parse_bet_budget(args.bet_budget)
    initial_budget = bet_budget if args.initial_bet_budget is None else parse_bet_budget(args.initial_bet_budget)
    early_budget = None if args.early_bet_budget is None else parse_bet_budget(args.early_bet_budget)
    maximum_budget = max(initial_budget, bet_budget, early_budget or Decimal(0))
    if maximum_budget > total:
        raise CLIError('An entry budget cannot exceed the cumulative spending ceiling')
    ladder = args.entry_schedule == 'every-minute'
    market_budget = (initial_budget + (schedule.main_entry_count - 1) * bet_budget +
                     schedule.early_entry_count * (early_budget or Decimal(0))) if ladder else bet_budget
    if profile is not None:
        schedule, slot_budgets, market_budget = recurring_budget_plan(bet_budget,
            warmup_seconds=args.warmup_seconds, cadence_seconds=args.bet_every_seconds,
            **_time_sizing_settings(profile))
        bet_budget = maximum_budget = max(slot_budgets)
    quantity_budget = maximum_budget
    if args._uniform_pacing_config is not None:
        maximum_budget = args._uniform_pacing_config.order_cap(slot_budgets)
    if ladder and total < market_budget:
        if profile is not None:
            raise CLIError(f'The cumulative ceiling must cover the {args.sizing_profile.capitalize()} market risk cap of ${budget_text(market_budget)}')
        raise CLIError(f'Recurring entries require a cumulative ceiling of at least ${budget_text(market_budget)} '
                       f'for an initial ${budget_text(initial_budget)} entry and '
                       f'{schedule.main_entry_count - 1} later entries of up to ${budget_text(bet_budget)} each' +
                       (f', plus {schedule.early_entry_count} early entries of up to ${budget_text(early_budget)} each'
                        if early_budget is not None else ''))
    if ladder and args.interval > args.bet_every_seconds:
        raise CLIError('Polling interval cannot exceed the configured betting cadence')
    fee = decimal_value(args.fee_reserve_per_contract, 'fee allowance', maximum=Decimal(1))
    if not 0 < fee < 1:
        raise CLIError('The per-contract fee allowance must be positive and less than one dollar')
    max_contracts = args.max_contracts
    if max_contracts is None:
        max_contracts = 5 if bet_budget == 1 and not ladder else min(10000, max(1, int(quantity_budget / fee)))
    assets = tuple(args.asset or ('BTC',))
    if len(assets) != len(set(assets)):
        raise CLIError('Choose each asset only once')
    risk = RiskConfig(max_total_cost=total, max_order_cost=maximum_budget,
        max_market_cost=market_budget, max_contracts=max_contracts,
        fee_reserve_per_contract=args.fee_reserve_per_contract, sizing_mode='fixed',
        max_signal_age_seconds=5, max_quote_age_seconds=5, min_seconds_to_close=1 if ladder else 60,
        required_series=asset_spec(assets[0]).series if len(assets) == 1 else None)
    if risk.fee_reserve_per_contract >= 1:
        raise CLIError('The per-contract fee allowance must be less than one dollar')
    args._entry_controls = _entry_controls(args, assets, risk)
    journal_path = _private_path(args.journal)
    report_path = None if args.report is None else _private_path(args.report)
    stop_path = None if args.stop_file is None else _private_path(args.stop_file)
    args._activity_report_path = None if args.activity_report is None else _private_path(args.activity_report)
    args._research_dir = None if args.research_dir is None else _private_path(args.research_dir)
    if args.research_run_id is not None and args._research_dir is None:
        raise CLIError('A research run identifier requires --research-dir')
    try:
        args._research_run_id = uuid.uuid4().hex if args.research_run_id is None else uuid.UUID(args.research_run_id).hex
    except (ValueError, TypeError, AttributeError):
        raise CLIError('Use a valid UUID research run identifier') from None
    args._status_report_path = (None if report_path is None or args.report_mode == 'sequential'
        or not args.run or args.observation_worker else _private_path(status_report_path(report_path)))
    selected_paths = (journal_path, report_path, stop_path, args._activity_report_path, args._status_report_path)
    args._read_coordination_path = (None if not args.parallel_assets else
        _private_path(journal_path.parent / 'read-coordination-v1.sqlite3'))
    if args._read_coordination_path is not None and args._read_coordination_path in selected_paths:
        raise CLIError('Shared read coordination must be separate from financial journals and reports')
    if len({p for p in selected_paths if p is not None}) != sum(p is not None for p in selected_paths):
        raise CLIError('Journal, report and stop file must use different paths')
    if args._research_dir is not None:
        if args._research_dir.exists() and not args._research_dir.is_dir():
            raise CLIError('Research data requires a separate directory')
        if any(path is not None and (path == args._research_dir or args._research_dir in path.parents
                or path in args._research_dir.parents) for path in selected_paths):
            raise CLIError('Keep research storage separate from the journal, reports and stop file')
    fit = None if args.probability_fit is None or args.show_sizing_plan else load_fitted_parameters(_private_path(args.probability_fit))
    from kalshi.asset_fits import load_asset_probability_fits
    fit_by_asset = ({} if args.show_sizing_plan else load_asset_probability_fits(
        bindings=args.asset_probability_fit, assets=tuple(asset for asset in assets if asset != 'GOLD')))
    if fit is not None and fit.asset in fit_by_asset:
        raise CLIError('Choose one probability fit per asset; do not repeat it across fit options')
    if fit is not None and fit.asset in assets:
        fit_by_asset[fit.asset] = fit
    if garch_path is not None and 'ETH' in fit_by_asset:
        raise CLIError('An ETH GARCH model cannot use an ETH legacy probability fit')
    args._asset_probability_fits = fit_by_asset
    matching_fit = fit if fit is not None and fit.asset in assets else None
    crypto_assets = tuple(asset for asset in assets if asset != 'GOLD')
    history = args.history_seconds if args.history_seconds is not None else (
        matching_fit.history_window_seconds if matching_fit is not None and len(crypto_assets) == 1 else 1800)
    if (matching_fit is not None and args.history_seconds is not None
            and history != matching_fit.history_window_seconds):
        raise CLIError('The requested model history differs from the loaded fit; refit using that history window or omit --history-seconds')
    for profile_fit in fit_by_asset.values():
        if args.history_seconds is not None and history != profile_fit.history_window_seconds:
            raise CLIError('An asset probability fit differs from --history-seconds; omit the global history override or refit that asset')
        if regime_config is not None and profile_fit.history_window_seconds < regime_config.lookback_seconds:
            raise CLIError('Each fitted history window must cover the requested regime lookback')
    if regime_config is not None and history < regime_config.lookback_seconds:
        raise CLIError('The model archive history must cover the requested regime lookback; increase compatible model history or shorten --regime-lookback-seconds')
    minimum = args.min_history_seconds if args.min_history_seconds is not None else (
        max(150, min(300 if profile is not None else args.warmup_seconds, history)) if ladder else 150)
    config = ForecastConfig(paths=args.paths, min_history_seconds=minimum, max_history_seconds=history)
    calibration_paths = _calibration_bindings(args, assets)
    model_inputs = set(calibration_paths.values())
    if args._eth_garch_model_path is not None:
        model_inputs.add(args._eth_garch_model_path)
    if args.probability_fit is not None:
        model_inputs.add(_private_path(args.probability_fit))
    for binding in args.asset_probability_fit:
        _, separator, supplied = binding.partition('=')
        if separator and supplied:
            model_inputs.add(_private_path(Path(supplied)))
    if model_inputs.intersection(p for p in selected_paths if p is not None):
        raise CLIError('Probability fit and calibration inputs must differ from the journal, reports and stop file')
    args._probability_calibration_paths = calibration_paths
    args._probability_calibrations = {}
    if not args.show_sizing_plan:
        if args._eth_garch_model_path is not None:
            model = load_live_garch_model(args._eth_garch_model_path, paths=args.paths, seed=config.seed)
            bound = model.base_model_definition['config']['forecast']
            if args.history_seconds is not None and args.history_seconds != bound['max_history_seconds']:
                raise CLIError('The history override differs from the ETH GARCH base; omit --history-seconds')
            if args.min_history_seconds is not None and args.min_history_seconds != bound['min_history_seconds']:
                raise CLIError('The minimum history override differs from the ETH GARCH base')
            minute_five_model_definition(config, asset='ETH', decision_model='garch', garch_model=model)
            args._eth_garch_model = model
        for asset, calibration_path in calibration_paths.items():
            model = load_calibration_model(calibration_path, asset=asset)
            if model.base_decision_model == 'preclose':
                bound = model.base_model_definition['config']['forecast']
                if args.history_seconds is not None and args.history_seconds != bound['max_history_seconds']:
                    raise CLIError('The history override differs from an asset calibration; omit --history-seconds to load each selected history')
                if args.min_history_seconds is not None and args.min_history_seconds != bound['min_history_seconds']:
                    raise CLIError('The minimum history override differs from the selected asset calibration')
            minute_five_model_definition(config, asset=asset, probability_fit=fit_by_asset.get(asset),
                decision_model='calibrated', probability_calibration=model)
            args._probability_calibrations[asset] = model
    # Apply fitted parameters inside the per-asset loop. A BTC fit must not
    # change another asset's EWMA half-life or minimum history requirement.
    credentials = {'key_id': os.environ.get('KALSHI_API_KEY_ID'),
                   'private_key_path': os.environ.get('KALSHI_PRIVATE_KEY_PATH')}
    if not args.show_sizing_plan and not all(credentials.values()):
        raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally')
    return assets, risk, config, journal_path, report_path, stop_path, fit, credentials, bet_budget


def _execution_quality_suffix(execution):
    """Display bounded evidence without attributing an unknown venue cause."""
    quality = execution.get('execution_quality')
    if not isinstance(quality, dict):
        return ''
    parts = []
    for key, label, maximum, places, unit in (
            ('economic_limit_price', 'limit', Decimal(1), 4, 'dollars'),
            ('observed_selected_ask', 'checked ask', Decimal(1), 4, 'dollars'),
            ('book_age_at_send_seconds', 'book age at send', Decimal(86400), 3, 'seconds'),
            ('send_to_ack_seconds', 'send-to-ack', Decimal(86400), 3, 'seconds')):
        try:
            raw = quality.get(key)
            if raw is None:
                raw = quality.get({'economic_limit_price': 'economic_limit',
                    'observed_selected_ask': 'observed_ask'}.get(key, key))
            if type(raw) is float:
                if not math.isfinite(raw):
                    continue
                raw = str(raw)
            if isinstance(raw, str) and len(raw) > 100:
                continue
            value = decimal_value(raw, label, places=30, maximum=maximum)
        except (EngineError, ValueError, TypeError):
            continue
        rendered = f'${value:.{places}f}' if unit == 'dollars' else f'{value:.{places}f}s'
        parts.append(label + ' ' + rendered)
    if execution.get('status') == 'canceled' and quality.get('fill_classification') == 'unclassified_zero_fill':
        parts.append('venue cancellation reason unavailable')
    return ''.join('; ' + part for part in parts)


def _gold_feed_state(stream, now=None):
    """A subscription acknowledgement alone is not a live data entitlement check."""
    now = time.time() if now is None else now
    state = stream.snapshot()
    observations = state.get('observations', ()) if isinstance(state, dict) else ()
    ready = False
    if observations:
        latest = observations[-1]
        try:
            ready = (state.get('status') == 'ready' and latest.source == 'PYTH_1OZGOLD_USD'
                and latest.underlying == 'Metal.Index.1OZGOLD/USD'
                and all(type(value) in (int, float) and math.isfinite(value)
                        for value in (latest.event_time, latest.received_at, latest.price))
                and latest.price > 0 and 0 <= now - latest.event_time <= 5
                and 0 <= now - latest.received_at <= 5)
        except AttributeError:
            pass
    return dict(kind='kalshi_gold_feed_preflight', version=1,
        status='ready' if ready else 'unavailable', execution_eligible=False,
        underlying='Metal.Index.1OZGOLD/USD', history_samples=len(observations),
        reason='Fresh matching Gold ticks received; model warmup is separate' if ready else
               'No fresh matching Gold ticks; verify existing Pyth feed access and connectivity')


class _UnavailableGoldStream:
    def snapshot(self):
        return dict(status='unavailable', observations=[], reason='Gold live Pyth connection is unavailable')


class _UnavailableGoldObserver:
    def sample(self):
        return dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
            strategy='minute_five', asset='GOLD', status='unavailable',
            reason='Gold feed or independent model archive is unavailable',
            fetched_at=datetime.now(timezone.utc).isoformat())


class _ObservationProxy:
    def __init__(self, pool, asset):
        self.pool, self.asset = pool, asset

    def sample(self):
        return self.pool.sample(self.asset)


class _ActivityReporter:
    """Fetch actual fills without pausing the trading coordinator or sharing SQLite."""
    def __init__(self, client, journal, path, interval, research=None):
        self.client, self.journal, self.path, self.interval = client, journal, path, interval
        self.research = research
        self.journal_id = hashlib.sha256(str(getattr(journal, 'path', '')).encode()).hexdigest()
        self.account_fingerprint = journal._meta('account_fingerprint')
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kalshi-activity-read')
        self.pending, self.next_due = None, 0
        self.status = dict(status='pending', reason='Actual venue fill history has not been fetched')

    def __enter__(self):
        return self

    def __exit__(self, *error):
        if self.pending is not None and not self.pending.done():
            emit_research(self.research, 'capture_shutdown_gap', dict(
                component='activity_audit', reason='An audit was still in flight at coordinator shutdown'))
        elif self.pending is not None:
            try:
                emit_research(self.research, 'activity_audit', dict(
                    report=self.pending.result(), journal_id=self.journal_id,
                    account_fingerprint=self.account_fingerprint))
            except Exception:
                emit_research(self.research, 'capture_shutdown_gap', dict(
                    component='activity_audit', reason='The final audit result was unavailable'))
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _refresh(self, snapshot):
        with self.client.read_only_clone() as reader:
            return ScheduledActivityAudit(reader, snapshot).refresh()

    def poll(self):
        now = time.monotonic()
        try:
            if self.pending is not None and self.pending.done():
                report = self.pending.result()
                emit_research(self.research, 'activity_audit', dict(report=report, journal_id=self.journal_id,
                    account_fingerprint=self.account_fingerprint))
                if self.path is not None:
                    _write_report(self.path, report)
                self.status = dict(status=report.get('status', 'unavailable'),
                    generated_at=report.get('generated_at'), reason=report.get('reason'))
                self.pending = None
            if self.pending is None and now >= self.next_due:
                snapshot = capture_scheduled_journal(self.journal)
                self.pending = self.pool.submit(copy_context().run, self._refresh, snapshot)
                self.next_due = now + self.interval
        except Exception:
            self.pending = None
            self.next_due = now + self.interval
            self.status = dict(status='unavailable', reason='Actual-fill audit or report output is unavailable')
        return dict(self.status)


def _read_transport_settings(args):
    if args._read_coordination_path is None:
        return {}
    from kalshi.read_coordination import SharedReadCoordinator
    return dict(read_coordinator=SharedReadCoordinator(args._read_coordination_path,
        read_interval=args.read_min_interval))


def _read_coordination_status(client, args):
    from kalshi.read_coordination import SharedReadCoordinator
    coordinator = getattr(client, '_read_coordinator', None)
    if isinstance(coordinator, SharedReadCoordinator):
        return coordinator.snapshot()
    return dict(kind='kalshi_shared_read_coordination', version=1,
        read_min_interval_seconds=args.read_min_interval, balance_min_interval_seconds=1,
        configured_cap_is_venue_budget=False, counters_are_producer_local=True)


def _start_research(args, resources, assets, risk, config, *, role):
    if args._research_dir is None:
        return None
    from kalshi.research_store import ResearchRecorder
    model_assets = ((args.worker_asset,) if args.observation_worker and (
                        args.decision_model == 'calibrated' or args.eth_garch_model is not None)
                    else assets)
    models = {asset: minute_five_model_definition(config, asset=asset,
        probability_fit=args._asset_probability_fits.get(asset), **_decision_settings(args, asset))
        for asset in model_assets if asset != 'GOLD'}
    excluded = {'probability_fit', 'asset_probability_fit', 'probability_calibration', 'eth_garch_model',
                'research_run_id', 'observation_session'}
    settings = {name: value for name, value in vars(args).items()
        if not name.startswith('_') and name not in excluded
        and (value is None or type(value) in (str, int, float, bool, list))}
    metadata = dict(strategy='scheduled', assets=list(assets), mode='live' if args.run else 'observation',
        risk=risk.to_dict(), settings=settings, models=models, source_fingerprints=source_fingerprints(),
        journal_id=hashlib.sha256(str(args.journal.expanduser().absolute()).encode()).hexdigest(),
        limitations=['Separate research recording is not an execution journal or authority.',
            'Bounded asynchronous capture reports queue loss and write failures; it is not guaranteed lossless.',
            'REST requests are captured; direct WebSocket frames require their own capture adapters.'])
    if role == 'coordinator':
        metadata['expected_producers'] = [dict(role='coordinator', asset=None)]
        if args.parallel_assets and args.run:
            metadata['expected_producers'].extend(dict(role='observation_worker', asset=asset) for asset in assets)
    return resources.enter_context(ResearchRecorder(args._research_dir, run_id=args._research_run_id,
        role=role, asset=args.worker_asset if args.observation_worker else None, metadata=metadata))


def _run_observation_worker(args, assets, risk, config, path, report_path, stop_path,
                            credentials, bet_budget, schedule, stake_settings):
    """A model-only child: no execution journal, account preflight or writes."""
    asset = args.worker_asset
    try:
        with ExitStack() as resources:
            research = _start_research(args, resources, assets, risk, config, role='observation_worker')
            reader = resources.enter_context(KalshiClient(environment='production',
                allow_writes=False, timeout=args.timeout, **credentials,
                **({} if research is None else dict(research=research)), **_read_transport_settings(args)))
            asset_fit = args._asset_probability_fits.get(asset)
            actual = minute_five_forecast_config(config, asset=asset, probability_fit=asset_fit,
                probability_calibration=args._probability_calibrations.get(asset), **_garch_settings(args, asset))
            definition = minute_five_model_definition(actual, asset=asset, probability_fit=asset_fit,
                **_decision_settings(args, asset))
            digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:20]
            archive_path = path.parent / (path.stem + '-forecasts') / asset / (digest + '.sqlite3')
            archive = resources.enter_context(ShadowArchive(archive_path, actual,
                model_definition=definition, asset=asset))
            observer = MinuteFiveObservationService(reader, archive, risk, assets=assets, asset=asset,
                config=actual, probability_fit=asset_fit, bet_budget=bet_budget,
                entry_schedule=args.entry_schedule, warmup_seconds=schedule.warmup_seconds,
                cadence_seconds=schedule.cadence_seconds, **stake_settings, **_decision_settings(args, asset))
            if research is not None:
                observer = ResearchObserver(observer, research, asset)
            selected_model = _decision_settings(args, asset).get('decision_model', 'monte-carlo')
            print(f'{asset} read-only observation worker active; decision model {selected_model}.', flush=True)
            while True:
                if stop_path is not None and stop_path.exists():
                    return 0
                started = time.monotonic()
                try:
                    observation = observer.sample()
                except Exception as error:
                    reason = (str(error) if isinstance(error, (EngineError, MinuteFiveError,
                        KalshiError, ShadowError, FittedParameterError)) else
                        'Read-only observation inputs are unavailable')
                    observation = dict(kind='kalshi_minute_five_observation', version=1,
                        observation_only=True, strategy='minute_five', asset=asset,
                        status='unavailable', reason=reason, fetched_at=time.time())
                _write_report(report_path, dict(kind='kalshi_parallel_observation', version=1,
                    session_id=args.observation_session, asset=asset, observed_at=time.time(),
                    observation=observation))
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print(f'{asset} read-only observation worker stopped.', flush=True)
        return 130


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(arguments)
    try:
        assets, risk, config, path, report_path, stop_path, fit, credentials, bet_budget = _configuration(args)
        if args._research_dir is not None and args.research_run_id is None:
            arguments.extend(('--research-run-id', args._research_run_id))
        profile = _time_sizing_profile(args)
        regime_config = _regime_configuration(args)
        rolling_kelly_config = _rolling_kelly_configuration(args)
        active_assets = assets
        entry_controls = args._entry_controls
        entry_assets = entry_controls.get('trade_assets', assets)
        scheduled_policy_config = _scheduled_configuration(args)
        uniform_pacing_config = args._uniform_pacing_config
        schedule = EntrySchedule(args.warmup_seconds, args.bet_every_seconds,
            early_entries=args.early_bet_budget is not None)
        initial_budget = bet_budget if args.initial_bet_budget is None else parse_bet_budget(args.initial_bet_budget)
        early_budget = None if args.early_bet_budget is None else parse_bet_budget(args.early_bet_budget)
        stake_settings = {} if args.initial_bet_budget is None else dict(initial_bet_budget=initial_budget)
        if early_budget is not None:
            stake_settings['early_bet_budget'] = early_budget
        stake_settings.update(_time_sizing_settings(profile))
        if args.observation_worker:
            return _run_observation_worker(args, assets, risk, config, path, report_path, stop_path,
                credentials, bet_budget, schedule, stake_settings)
        mode_settings = dict(allowance_mode='continuous') if args.continuous else {}
        if uniform_pacing_config is not None:
            mode_settings['uniform_pacing_config'] = uniform_pacing_config
        if scheduled_policy_config is not None:
            mode_settings['scheduled_policy_config'] = scheduled_policy_config
        if rolling_kelly_config is not None:
            mode_settings['rolling_kelly_config'] = rolling_kelly_config
        if args.continue_unresolved:
            mode_settings['continue_unresolved'] = True
        if args.skip_probability_at_or_above is not None:
            mode_settings['skip_probability_at_or_above'] = parse_probability_ceiling(args.skip_probability_at_or_above)
        if args.exit_bid_above is not None:
            mode_settings['exit_bid_above'] = parse_exit_bid_threshold(args.exit_bid_above)
            mode_settings['exit_seconds_before_close'] = args.exit_seconds_before_close
        if args.stop_bid_at_or_below is not None:
            mode_settings['stop_bid_at_or_below'] = parse_exit_bid_threshold(args.stop_bid_at_or_below)
            mode_settings['stop_seconds_before_close'] = args.stop_seconds_before_close
        if args.opposite_bid_at_or_below is not None:
            mode_settings['opposite_bid_at_or_below'] = parse_exit_bid_threshold(args.opposite_bid_at_or_below)
            mode_settings['opposite_after_seconds'] = args.opposite_after_seconds
        if args.late_model_exit_probability_above is not None:
            mode_settings['late_model_exit_probability_above'] = parse_exit_bid_threshold(args.late_model_exit_probability_above)
            mode_settings['late_model_exit_seconds_before_close'] = args.late_model_exit_seconds_before_close
            if args.eth_late_model_direction != 'first-fresh':
                mode_settings['eth_late_model_direction'] = args.eth_late_model_direction
        if args.resume_after_exit:
            mode_settings['resume_after_exit'] = True
        if regime_config is not None:
            mode_settings['regime_config'] = regime_config
        ceiling_label = 'active reservation' if args.continuous else 'cumulative'
        cash_settings = (dict(cash_allocation=args.cash_allocation)
            if args.update_cash_allocation and (args.update_entry_schedule or args.update_risk_limits) else {})
        if args.show_sizing_plan:
            if args.entry_schedule == 'every-minute':
                schedule, slot_budgets, market_budget = recurring_budget_plan(bet_budget,
                    warmup_seconds=args.warmup_seconds, cadence_seconds=args.bet_every_seconds, **stake_settings)
                entries = [dict(slot_index=slot, elapsed_start_seconds=schedule.slot_start(slot),
                    elapsed_end_seconds=schedule.slot_end(slot), budget=budget_text(amount))
                    for slot, amount in enumerate(slot_budgets)]
            else:
                market_budget, slot_budgets = bet_budget, (bet_budget,)
                entries = [dict(slot_index=0, elapsed_start_seconds=300,
                    elapsed_end_seconds=360, budget=budget_text(bet_budget))]
            planned = sum(slot_budgets, Decimal(0))
            plan = dict(kind='kalshi_time_sizing_plan', version=1, execution_eligible=False,
                assets=list(assets),
                sizing_profile=args.sizing_profile, parameters=None if profile is None else profile.to_dict(),
                market_budget=budget_text(market_budget), planned_total=budget_text(planned),
                unallocated_budget=format(market_budget - planned, 'f'), entries=entries)
            if entry_controls:
                plan.update(managed_assets=list(assets), entry_assets=list(entry_assets),
                    entry_contracts=entry_controls.get('entry_contracts'),
                    entry_quantity_scope='new_entries_only', runtime_entry_controls=True,
                    orders_required_each_window=False)
            if uniform_pacing_config is not None:
                for slot, entry in enumerate(entries):
                    entry['released_budget'] = budget_text(uniform_pacing_config.release(slot_budgets, slot))
                    entry['maximum_pacing_allowance'] = budget_text(
                        uniform_pacing_config.allowance(slot_budgets, slot, Decimal(0)))
                plan.update(uniform_pacing=uniform_pacing_config.to_dict(),
                    maximum_order_budget=budget_text(risk.max_order_cost),
                    pacing_release_basis='original_uniform_opportunity_slices',
                    orders_required_each_window=False)
            if args.parallel_assets:
                plan.update(parallel_assets=True, worker_assets=list(assets),
                    shared_reservation_ceiling=format(risk.max_total_cost, 'f'),
                    market_budget_scope='per_market', worker_processes_started=False,
                    worker_scope='market_observations_only', trading_coordinator_count=1)
            if args.decision_model != 'monte-carlo':
                plan['decision_model'] = args.decision_model
            if args.decision_model == 'calibrated':
                plan['probability_calibration_status'] = 'not_loaded_sizing_only'
            plan.update(_garch_report_fields(args, assets, sizing_only=True))
            if scheduled_policy_config is not None:
                plan['scheduled_policy'] = scheduled_policy_config.to_dict()
            if args._activity_report_path is not None:
                plan.update(actual_fill_audit_enabled=True, audit_interval_seconds=args.audit_interval_seconds)
            if rolling_kelly_config is not None:
                cumulative = Decimal(0)
                for entry, amount in zip(entries, slot_budgets):
                    cumulative += amount
                    entry['accrued_capital'] = format(cumulative, 'f')
                    entry['per_asset_accrued_capital'] = format(cumulative / len(assets), 'f')
                plan.update(rolling_kelly=rolling_kelly_config.to_dict(),
                    assets=list(assets), asset_count=len(assets),
                    capital_allocation='equal_asset_shares',
                    per_asset_capital_budget=format(market_budget / len(assets), 'f'),
                    capital_budget_scope='shared_selected_assets', market_budget_scope='per_market_risk_cap',
                    shared_reservation_ceiling=format(risk.max_total_cost, 'f'),
                    capital_basis='cumulative_scheduled_allowances',
                    probability_margin_status='configured_sensitivity_not_calibrated',
                    orders_required_each_window=False)
            if args.skip_probability_at_or_above is not None:
                plan['skip_probability_at_or_above'] = format(mode_settings['skip_probability_at_or_above'], 'f')
            if args.exit_bid_above is not None:
                plan.update(exit_bid_above=format(mode_settings['exit_bid_above'], 'f'),
                            exit_seconds_before_close=args.exit_seconds_before_close)
            if args.stop_bid_at_or_below is not None:
                plan.update(stop_bid_at_or_below=format(mode_settings['stop_bid_at_or_below'], 'f'),
                            stop_seconds_before_close=args.stop_seconds_before_close)
            if args.opposite_bid_at_or_below is not None:
                plan.update(opposite_bid_at_or_below=format(mode_settings['opposite_bid_at_or_below'], 'f'),
                            opposite_after_seconds=args.opposite_after_seconds)
            if args.late_model_exit_probability_above is not None:
                plan.update(late_model_exit_probability_above=format(mode_settings['late_model_exit_probability_above'], 'f'),
                            late_model_exit_seconds_before_close=args.late_model_exit_seconds_before_close)
                if args.eth_late_model_direction != 'first-fresh':
                    plan['eth_late_model_direction'] = args.eth_late_model_direction
            if args.resume_after_exit:
                plan.update(resume_after_exit=True, exit_reentry_cutoff_seconds=60)
            if regime_config is not None:
                plan['regime_switching'] = regime_config.to_dict()
            if report_path is not None:
                _write_report(report_path, plan)
            print(json.dumps(plan, allow_nan=False, indent=2), flush=True)
            return 0
        scheduling = {} if schedule == EntrySchedule() else dict(
            warmup_seconds=schedule.warmup_seconds, cadence_seconds=schedule.cadence_seconds)
        if args.run and stop_path is not None and stop_path.exists():
            print('Stop file found; no orders will be submitted.', flush=True)
            return 0
        with ExitStack() as resources:
            coordinator_lease = resources.enter_context(ParallelCoordinatorLease(path, assets=assets))
            research = _start_research(args, resources, assets, risk, config, role='coordinator')
            client = resources.enter_context(KalshiClient(environment='production',
                allow_writes=args.run, timeout=args.timeout, **credentials,
                **({} if research is None else dict(research=research)), **_read_transport_settings(args)))
            journal = resources.enter_context(Journal(path, mode='live', environment='production',
                risk=None if path.exists() else risk))
            with journal.exclusive():
                if not any((args.update_bet_budget, args.update_entry_schedule, args.update_risk_limits)):
                    if (journal.risk.to_dict() != risk.to_dict()
                            or journal._meta('minute_five_execution_policy') not in
                                (None, execution_policy(assets, bet_budget, entry_schedule=args.entry_schedule,
                                                        **scheduling, **stake_settings, **mode_settings))):
                        raise CLIError('Journal sizing or schedule differs. Stop the old runner; use --update-risk-limits for existing every-minute limits, --update-entry-schedule for an entry budget or timing change, or --update-bet-budget for once-only sizing')
                pending = [row for row in journal.intents() if row['status'] in ('sending', 'unknown')]
                continuation_migration = (args.continue_unresolved and args.update_entry_schedule
                    and journal.allowance_mode() == 'continuous'
                    and all(row['client_order_id'] in journal.bounded_pending_ioc_ids()
                        or row['client_order_id'] in journal.settled_exposure_intent_ids() for row in pending))
                if journal.halted() and not continuation_migration:
                    raise CLIError('An order is unresolved; reconcile this journal with tools/kalshi_reconcile.py --apply before starting again')
                preflight_bot = Bot(client, journal, journal.risk, mode='live', pause_on_manual_trade=True)
                cash = preflight_bot._account_cash(**({'allow_pending_ioc': True} if continuation_migration else {}))
                if (args.continuous and not any((args.update_entry_schedule, args.update_risk_limits))
                        and journal._meta('minute_five_execution_policy') is None):
                    if journal.intents():
                        raise CLIError('Existing order history has no saved entry policy; reconcile before changing allowances')
                    with journal.connection:
                        journal.connection.execute('INSERT INTO kalshi_meta VALUES (?,?)',
                            ('minute_five_execution_policy', execution_policy(assets, bet_budget,
                                entry_schedule=args.entry_schedule, **scheduling, **stake_settings, **mode_settings)))
            if args.update_risk_limits:
                update_risk_limits(journal, risk, assets=assets, bet_budget=bet_budget,
                    entry_schedule=args.entry_schedule, account_fingerprint=client.account_fingerprint,
                    **scheduling, **stake_settings, **cash_settings, **mode_settings)
                print(f'Risk limits applied: {risk.max_contracts} contracts per order; '
                    f'${risk.max_total_cost} {ceiling_label} ceiling. Prior spending and orders preserved.', flush=True)
            elif args.update_entry_schedule:
                update_entry_schedule(journal, risk, assets=assets, bet_budget=bet_budget,
                    entry_schedule=args.entry_schedule, account_fingerprint=client.account_fingerprint,
                    **scheduling, **stake_settings, **cash_settings, **mode_settings,
                    **({'allow_asset_expansion': True} if args.add_assets else {}))
                print(f'Every-minute schedule applied; ${risk.max_total_cost} {ceiling_label} ceiling. '
                      'Prior orders and cumulative spending history preserved.', flush=True)
            elif args.update_bet_budget:
                change = update_bet_budget(journal, risk, assets=assets, bet_budget=bet_budget,
                    account_fingerprint=client.account_fingerprint)
                print(f"Per-bet budget: ${change['budget']}; prior orders and cumulative spending history preserved.", flush=True)
            elif args.update_cash_allocation:
                update_cash_allocation(journal, args.cash_allocation,
                    account_fingerprint=client.account_fingerprint)
                with journal.exclusive():
                    cash_details = dict(preflight_bot._account_cash_details)
                    allocation = journal.cash_allocation()
                    committed = journal.allowance_committed()
                    cash = journal.cash_budget(Decimal(cash_details['venue_cash']))
                    cash_details.update(cash_allocation=format(allocation, 'f'),
                        cash_allowance_remaining=format(max(Decimal(0), allocation - committed), 'f'),
                        strategy_cash=format(cash, 'f'), committed_cost=format(committed, 'f'))
                    preflight_bot._account_cash_details = cash_details
                print(f'Cash allocation applied: ${budget_text(allocation)} total; '
                      'prior spending, orders and original starting cash preserved.', flush=True)
            reader = None
            if args.continuous:
                reader = resources.enter_context(client.read_only_clone())
                if args.continue_unresolved and journal.bounded_pending_ioc_ids():
                    try:
                        verify_settled_exposure(reader, journal, apply=True, max_pages=2, max_markets=1)
                    except EngineError:
                        # Missing settlement evidence retains the full pending
                        # reservation; it does not stop unrelated markets.
                        pass
                with journal.exclusive():
                    settlement = refresh_settled_allowances(reader, journal)
                    # Re-read authenticated cash after settlement checks and policy
                    # changes; saved initial cash remains an audit baseline.
                    preflight_bot = Bot(client, journal, journal.risk, mode='live', pause_on_manual_trade=True)
                    cash = preflight_bot._account_cash()
                print(f"Continuous allowance enabled: active reserved ${journal.active_committed()}; "
                      f"lifetime reserved ${journal.committed()}; "
                      f"released ${settlement['released_cost']} after confirmed settlement.", flush=True)
            cash_details = preflight_bot._account_cash_details
            allowances = journal.allowance_snapshot(venue_cash=Decimal(cash_details['venue_cash']))
            cash = Decimal(allowances['effective_remaining'])
            print(f'Account preflight passed. Available strategy cash: ${cash}; '
                  f'{ceiling_label} ceiling: ${risk.max_total_cost}; reserved: ${journal.allowance_committed()}.', flush=True)
            print(f"Cash limits: live account cash ${cash_details['venue_cash']}; "
                  f"saved initial cash ${cash_details['initial_cash']}; "
                  f"cash allocation ${allowances['cash_allocation']}; "
                  f"remaining cash allowance ${allowances['cash_allowance_remaining']}; "
                  f"remaining {ceiling_label} allowance ${allowances['cumulative_remaining']}; "
                  f"per-market risk cap ${allowances['market_ceiling']}.", flush=True)
            gold_stream, gold_feed = None, None
            if 'GOLD' in assets and (args.run or args.check_gold_feed):
                try:
                    gold_stream = resources.enter_context(KalshiPythStream(
                        credentials['key_id'], credentials['private_key_path'], history_seconds=3600))
                except Exception:
                    gold_stream = _UnavailableGoldStream()
                if args.check_gold_feed:
                    deadline = time.monotonic() + args.gold_feed_wait_seconds
                    while True:
                        gold_feed = _gold_feed_state(gold_stream)
                        if gold_feed['status'] == 'ready' or time.monotonic() >= deadline:
                            break
                        time.sleep(min(.05, max(0, deadline - time.monotonic())))
                    print(json.dumps(gold_feed, allow_nan=False), flush=True)
            elif 'GOLD' in assets:
                print('Gold live Pyth access has not been checked. Use --check-gold-feed to verify actual fresh ticks; '
                      'the BTC fit is not used for Gold.', flush=True)
            if entry_controls:
                print('New entry assets: ' + ', '.join(entry_assets) +
                      '; journal and protective-exit assets: ' + ', '.join(assets) + '. '
                      'This is a runtime restriction; repeat --trade-asset on restart.', flush=True)
                if args.entry_contracts is not None:
                    quantity_label = 'contract' if args.entry_contracts == 1 else 'contracts'
                    print(f'Entry quantity: request exactly {args.entry_contracts} {quantity_label} when the full quantity '
                          'passes the configured entry filters, cash, risk and executable depth checks; otherwise skip. '
                          'IOC fills may be partial; protective exit sizing is unchanged.', flush=True)
            if not args.run:
                if args._eth_garch_model is not None:
                    print('ETH Gaussian GARCH probability override validated; frozen historical fit, not live '
                          'validated. Forecasts require 120 completed native minute returns; no observation '
                          'workers started during preflight.', flush=True)
                if args.parallel_assets:
                    print('Parallel plan: independent workers for ' + ', '.join(assets) +
                          f'; ${risk.max_market_cost:f} per-market risk cap and '
                          f'${risk.max_total_cost:f} shared {ceiling_label} ceiling. '
                          'No worker processes started during read-only preflight.', flush=True)
                print('Read-only preflight complete. No orders submitted.', flush=True)
                return 2 if gold_feed is not None and gold_feed['status'] != 'ready' else 0
            if args.continue_unresolved:
                print('Unresolved IOC continuation enabled: affected contracts are quarantined; '
                      'their full reservations remain charged. Other markets remain eligible within cash and risk limits.', flush=True)
            if args._eth_garch_model is not None:
                print('ETH probability override: frozen Gaussian GARCH(1,1) with its fitted probability map. '
                      'Other assets retain their selected model. Native minute variance is filtered causally; '
                      '120 completed returns are required. A new archive may need another hour beyond the '
                      'feed\'s initial rolling hour. This historical fit is not prospectively live validated.', flush=True)
            if args.skip_probability_at_or_above is not None:
                ceiling = mode_settings['skip_probability_at_or_above']
                estimator_label = {'analytic': 'analytic', 'calibrated': 'calibrated'}.get(args.decision_model, 'Monte Carlo')
                print(f'Probability entry filter: selected-side {estimator_label} probability at or above {ceiling * 100:g}% '
                      'is skipped before an order attempt.', flush=True)
            else:
                print('Upper model-probability entry ceiling disabled; a high selected-side model probability '
                      'does not reject an entry. Cash, liquidity, freshness and exit rules still apply.', flush=True)
            if args.exit_bid_above is not None:
                threshold = mode_settings['exit_bid_above']
                exit_window = ('throughout the open 15-minute contract' if args.exit_seconds_before_close == 900
                    else f'in the final {args.exit_seconds_before_close} seconds')
                reentry = ('confirmed full early exits may resume entries on the next scheduled window only while '
                    'more than 60 seconds remain; no reopening during the final minute.' if args.resume_after_exit else
                    'a triggered market cannot be reopened.')
                print(f'Price exits armed {exit_window}: reduce-only IOC sales of journal-owned YES or NO holdings '
                      f'when their executable bid is strictly above ${threshold:f}, before fees. '
                      'Exits are checked on every poll before model entries, with a one-second submission cutoff; '
                      + reentry, flush=True)
            if args.stop_bid_at_or_below is not None:
                threshold = mode_settings['stop_bid_at_or_below']
                print(f'Late bid stop armed: attempt to close the entire journal-owned YES or NO position '
                      f'when its executable bid is at or below ${threshold:f} and strictly fewer than '
                      f'{args.stop_seconds_before_close} seconds remain, with a one-second submission cutoff. '
                      'The stop is checked on every poll before model entries. Once triggered, reduce-only IOC '
                      'liquidation continues as liquidity permits even if the bid recovers; the stopped contract '
                      'cannot be reopened.', flush=True)
            if args.opposite_bid_at_or_below is not None:
                threshold = mode_settings['opposite_bid_at_or_below']
                print(f'Opposite-side targeting armed from elapsed {args.opposite_after_seconds}s: '
                      f'a held-side executable bid at or below ${threshold:f} switches the persistent purchase '
                      'target to the opposite outcome. Scheduled purchases keep the existing cadence and fee-inclusive '
                      'slot budgets, reducing old exposure before opening the opposite side. The target persists '
                      'until the newly held side reaches the same bid threshold. Profit exits and late stops retain '
                      'priority; an armed target overrides model direction and the ranging 50-cent exit.', flush=True)
            if args.late_model_exit_probability_above is not None:
                threshold = mode_settings['late_model_exit_probability_above']
                direction_description = (
                    'ETH uses the latest fresh matched strict-majority YES or NO direction on each eligible check. '
                    'Ties and stale forecasts cannot create a new model-exit trigger. '
                    'Other assets retain their first fresh direction. '
                    if args.eth_late_model_direction == 'latest-fresh' else
                    'The first fresh matched forecast fixes its strict-majority YES or NO direction. ')
                print(f'Late model-direction exit armed with {args.late_model_exit_seconds_before_close} seconds '
                      'or fewer remaining: ' + direction_description +
                      f'If the selected direction probability exceeds {threshold * 100:g}%, '
                      'close an opposing journal-owned holding with reduce-only IOC sales. '
                      'Same-direction holdings stay open under the other exit rules. A triggered '
                      'liquidation remains latched and its contract cannot reopen. '
                      'This threshold uses model probability, not the market bid.', flush=True)
            if regime_config is not None:
                range_behavior = (f'Valid directional scores strictly between -{regime_config.trend_score:g} '
                    f'and +{regime_config.trend_score:g} classify as ranging, without efficiency or volatility '
                    'range filters. Exact trend thresholds retain their existing trend-efficiency checks. '
                    if regime_config.range_rule == 'between_trends' else '')
                transition_behavior = ('Valid transition regimes use normal Monte Carlo side selection, '
                    'without a ranging target; unavailable evidence still blocks new regime entries. '
                    if regime_config.transition_action == 'model' else
                    'Transition or unavailable evidence blocks new regime entries. ')
                regime_assets = ', '.join(assets)
                regime_sources = 'CF BRTI' if assets == ('BTC',) else 'each asset\'s exact CF index'
                print(f'Experimental {regime_assets} regime policy armed: classify before the first entry using '
                      f'{regime_config.lookback_seconds:g}s of {regime_sources} history with a '
                      f'{regime_config.half_life_seconds:g}s EWMA half-life; each contract keeps its initial regime. '
                      'Bullish and bearish regimes retain normal Monte Carlo side selection. Ranging positions '
                      'close at an executable held-side bid of at least $0.50; the opposite target can enter '
                      'on a later scheduled window only if it clears round-trip fee allowances and the entry margin. '
                      + range_behavior + transition_behavior +
                      'This is a mean-reversion experiment, not a delta-neutral hedge.', flush=True)
            print('Manual/external fill protection enabled: a detected non-journal fill pauses all bot entries '
                  'and exits for that exact 15-minute contract, including after restart.', flush=True)
            if scheduled_policy_config is not None:
                print('Scheduled execution policy: ' + (
                    'executable edge filter disabled' if scheduled_policy_config.min_entry_edge is None else
                    f'minimum entry edge {scheduled_policy_config.min_entry_edge:f}') +
                    f'; probability sensitivity allowance {scheduled_policy_config.probability_margin:f}; '
                    f'per-contract slippage allowance {scheduled_policy_config.slippage:f}; '
                    f'exit cooldown {scheduled_policy_config.min_exit_cooldown_seconds}s; '
                    f're-entry requires probability movement {scheduled_policy_config.reentry_probability_move:f}. '
                    'The sensitivity allowance is configured, not a calibrated confidence bound.', flush=True)
                if scheduled_policy_config.min_entry_edge == Decimal(0):
                    print('Zero minimum entry edge keeps the fee-adjusted filter enabled: '
                          'the selected outcome must still have strictly positive edge after fee bounds. '
                          'Omit --scheduled-entry-edge to restore the original scheduled side selection.', flush=True)
            if rolling_kelly_config is not None:
                print('Rolling aggregate Kelly armed: uniform windows release capital cumulatively; '
                      'each decision targets the whole confirmed position, including earlier journal fills. '
                      f'Kelly fraction {rolling_kelly_config.fraction:f}; minimum entry edge '
                      f'{rolling_kelly_config.min_entry_edge:f}; probability sensitivity margin '
                      f'{rolling_kelly_config.probability_margin:f}. '
                      'The sensitivity margin is configured, not a calibrated confidence bound. '
                      'YES and NO executable edges are assessed separately; model-relative reduce-only exits '
                      'and target reductions use fresh forecasts. Emergency late stops and cash, exposure, '
                      'ownership and freshness limits remain active. A window does not require a purchase.', flush=True)
                if len(assets) > 1:
                    print(f'Multi-asset rolling targets share one ${risk.max_total_cost:f} '
                          f'{ceiling_label} ceiling and one strategy cash allowance. '
                          f'The ${profile.budget:f} uniform capital pool is divided equally across '
                          f'{len(assets)} assets: ${profile.budget / len(assets):f} per asset before trading P&L. '
                          f'The separate per-market reservation cap remains ${risk.max_market_cost:f}. '
                          'Assets are evaluated sequentially; '
                          'missed windows are not replayed and five-second freshness checks remain active.', flush=True)
            if uniform_pacing_config is not None:
                print(f'Cumulative uniform pacing: release the original ${bet_budget:f} slice per scheduled window; '
                      f'carry unused release up to ${risk.max_order_cost:f} per new order '
                      f'({uniform_pacing_config.carry_slots} slices). '
                      'All charged market reservations count toward release. '
                      'Market and shared cash limits, protective exits and consumed slots remain in force.', flush=True)
            with journal.exclusive():
                journal.ensure_execution_indexes()
            executor = MinuteFiveLiveExecutor(client, journal, risk,
                assets=assets, bet_budget=bet_budget,
                **({} if args.entry_schedule == 'once' else {'entry_schedule': args.entry_schedule}),
                pause_on_manual_trade=True, **scheduling, **stake_settings, **mode_settings,
                planning_read_mode=args.planning_read_mode, **entry_controls,
                **({} if args.decision_model != 'calibrated' else
                   dict(probability_calibrations=args._probability_calibrations)),
                **({} if args._eth_garch_model is None else dict(garch_models={'ETH': args._eth_garch_model})))
            resources.callback(executor.close)
            planning_read_mode = getattr(executor, 'planning_read_mode', None)
            if type(planning_read_mode) is not str or planning_read_mode not in ('parallel', 'sequential'):
                planning_read_mode = args.planning_read_mode
            if research is not None:
                executor = ResearchExecutor(executor, research)
                print(f'Research recording enabled; run {research.run_id}. '
                    'Capture health is separate from trading state.', flush=True)
            if reader is None:
                reader = resources.enter_context(client.read_only_clone())
            discovery_cache = None
            if not args.parallel_assets:
                discovery_cache = LocalDiscoveryCache(clock=time.time, monotonic=time.monotonic)
                reader._discovery_cache = discovery_cache
            services = {}
            observation_pool = None
            account_reader = None
            from kalshi.scheduled_telemetry import ScheduledTelemetry
            coordinator_mode = ('single_writer_parallel_observations' if args.parallel_assets else
                                'single_writer_local_observations')
            execution_metrics = ScheduledTelemetry(assets, coordinator_mode=coordinator_mode,
                account_read_mode='parallel_paced' if args.account_read_mode == 'parallel' else 'sequential')
            if args.account_read_mode == 'parallel':
                account_reader = resources.enter_context(ParallelAccountReader(client,
                    max_age_seconds=risk.max_quote_age_seconds))
                executor.bot.account_reader = account_reader
            executor.bot.account_read_observer = execution_metrics.record_account_read
            print('Account reads: ' + args.account_read_mode +
                  '; execution telemetry enabled, with one financial writer.', flush=True)
            print('Planning reads: ' + planning_read_mode + '; routine reports: ' + args.report_mode + '.', flush=True)
            if args.parallel_assets:
                observation_pool = resources.enter_context(ParallelObservationPool(arguments, assets=assets,
                    journal_path=path, report_path=report_path, stop_path=stop_path,
                    script_path=Path(__file__).resolve(), coordinator_lease=coordinator_lease))
                print('Read-only observation workers active for ' + ', '.join(assets) +
                      '; this parent owns every order and the shared execution journal.', flush=True)
            for asset in active_assets:
                if observation_pool is not None:
                    services[asset] = _ObservationProxy(observation_pool, asset)
                    continue
                if asset == 'GOLD':
                    # Gold has a terminal candle-close model and its own archive.
                    # BTC calibration/history requirements never leak into it.
                    gold_history = 1800 if args.history_seconds is None else args.history_seconds
                    gold_minimum = 300 if args.min_history_seconds is None else args.min_history_seconds
                    try:
                        gold_config = ForecastConfig(paths=args.paths, min_history_seconds=gold_minimum,
                            max_history_seconds=gold_history)
                        digest = hashlib.sha256(json.dumps(gold_model_definition(gold_config), sort_keys=True).encode()).hexdigest()[:20]
                        gold_path = path.parent / (path.stem + '-forecasts') / 'GOLD' / ('pyth-terminal-' + digest + '.sqlite3')
                        services[asset] = resources.enter_context(GoldObservationService(reader, gold_path, risk,
                            stream=gold_stream, config=gold_config, bet_budget=bet_budget,
                            entry_schedule=args.entry_schedule, **scheduling, **stake_settings))
                    except Exception:
                        services[asset] = _UnavailableGoldObserver()
                    print(f'GOLD model lookback: {gold_history:g}s; minimum usable live history: {gold_minimum:g}s; '
                          'independent Pyth terminal-close baseline, not the BTC fit.', flush=True)
                    continue
                asset_fit = args._asset_probability_fits.get(asset)
                actual = minute_five_forecast_config(config, asset=asset, probability_fit=asset_fit,
                    probability_calibration=args._probability_calibrations.get(asset), **_garch_settings(args, asset))
                definition = minute_five_model_definition(actual, asset=asset, probability_fit=asset_fit,
                    **_decision_settings(args, asset))
                digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:20]
                archive_path = path.parent / (path.stem + '-forecasts') / asset / (digest + '.sqlite3')
                archive = resources.enter_context(ShadowArchive(archive_path, actual,
                    model_definition=definition, asset=asset))
                services[asset] = MinuteFiveObservationService(reader, archive, risk,
                    assets=assets, asset=asset, config=actual, probability_fit=asset_fit, bet_budget=bet_budget,
                    **({} if args.entry_schedule == 'once' else {'entry_schedule': args.entry_schedule}),
                    **scheduling, **stake_settings,
                    **_decision_settings(args, asset),
                    **({} if regime_config is None else dict(regime_config=regime_config)))
                print(f'{asset} model lookback: {actual.max_history_seconds:g}s; '
                    f'minimum usable history: {actual.min_history_seconds:g}s.', flush=True)
                if rolling_kelly_config is not None:
                    fitted_here = asset_fit is not None
                    print(f'{asset} probability parameters: ' + (
                        'asset-matched research fit; not live-calibrated.' if fitted_here else
                        'asset-specific unfitted diffusion baseline; no other asset fit is applied.'), flush=True)
            if args.entry_schedule == 'every-minute':
                if isinstance(profile, GaussianSizing):
                    slot_budgets = profile.budgets(schedule)
                    planned = sum(slot_budgets, Decimal(0))
                    print('LIVE Gaussian entries armed for ' + ', '.join(entry_assets) +
                        f'. ${budget_text(profile.budget)} risk cap per market; ${budget_text(planned)} planned; '
                        f'${budget_text(profile.budget - planned) if profile.budget > planned else "0"} unallocated. '
                        f'Peak {profile.peak_seconds:g}s, width {profile.width_seconds:g}s; '
                        f'{schedule.entry_count} opportunities with a $1 minimum and whole-dollar budgets.', flush=True)
                elif isinstance(profile, UniformSizing):
                    slot_budgets = profile.budgets(schedule)
                    planned = sum(slot_budgets, Decimal(0))
                    print('LIVE uniform entries armed for ' + ', '.join(entry_assets) +
                        f'. ${budget_text(profile.budget)} risk cap per market; ${budget_text(planned)} planned; '
                        f'${format(profile.budget - planned, "f")} unallocated. '
                        f'{schedule.entry_count} equal fee-inclusive allowances of ${budget_text(slot_budgets[0])} '
                        f'every {schedule.cadence_seconds}s; actual fills use integer contracts. ' +
                        ('Scheduled allowances accumulate as the rolling exposure capital base; '
                         'individual order budgets and all reservation caps remain bounded.'
                         if rolling_kelly_config is not None else
                         f'Unused released allowances carry forward within the ${risk.max_order_cost:f} per-order cap.'
                         if uniform_pacing_config is not None else
                         'Unused allowances are not carried forward.'), flush=True)
                else:
                    entry_description = (f'One ${budget_text(bet_budget)} IOC attempt' if initial_budget == bet_budget else
                                         f'Initial ${budget_text(initial_budget)}, then ${budget_text(bet_budget)} IOC attempts')
                    print('LIVE recurring entries armed for ' + ', '.join(entry_assets) +
                        f'. {entry_description} every {schedule.cadence_seconds}s after {schedule.warmup_seconds}s of warmup; '
                        f'up to {schedule.entry_count} attempts and ${budget_text(risk.max_market_cost)} per market.', flush=True)
                if early_budget is not None:
                    print(f'Early phase: up to {schedule.early_entry_count} entries of ${budget_text(early_budget)} '
                          f'every 60s before elapsed {schedule.warmup_seconds}s; '
                          'model readiness and valid contract terms remain required. '
                          'The larger initial budget applies when the main phase starts.', flush=True)
                print('Opposite directions can reduce or reverse the existing net position. Missed minutes are not replayed.', flush=True)
                if args.continuous:
                    print('Continuous mode: confirmed final settlement releases working allowance. '
                          'The runner waits while allowance is reserved; live cash and each market cap still apply.', flush=True)
            else:
                print('LIVE minute-five entries armed for ' + ', '.join(entry_assets) +
                      f'. Maximum ${budget_text(bet_budget)} including fee allowance per market; one IOC attempt; hold to settlement.', flush=True)
            print('Collecting current CF history. Ctrl-C stops new entries and leaves filled positions open.', flush=True)
            if research is not None:
                services = {asset: ResearchObserver(service, research, asset) for asset, service in services.items()}
            reports, previous, first = {}, {}, 0
            previous_research_loss = None
            activity = (None if args._activity_report_path is None and research is None else resources.enter_context(
                _ActivityReporter(client, journal, args._activity_report_path, args.audit_interval_seconds,
                    **({} if research is None else dict(research=research)))))
            report_history = JournalReportCache()
            report_session_id = uuid.uuid4().hex
            paired_writer = (None if args._status_report_path is None else PairedReportWriter(
                args._status_report_path, writer=_write_report, clock=time.monotonic))
            report_publisher = (None if paired_writer is None else resources.enter_context(
                CoalescingReportPublisher(report_path, writer=paired_writer)))
            settlement_worker = (None if not args.continuous or args.settlement_mode == 'sequential' else
                resources.enter_context(BackgroundSettlement(client, clock=time.time, monotonic=time.monotonic,
                    max_age_seconds=risk.max_quote_age_seconds, continue_unresolved=args.continue_unresolved)))
            next_settlement_check = time.monotonic() + 15
            while True:
                if report_publisher is not None:
                    report_publisher.check_health()
                if stop_path is not None and stop_path.exists():
                    print('Stop file found; no further orders will be submitted.', flush=True)
                    return 0
                if journal.halted():
                    raise CLIError('Order outcome is uncertain; new submissions halted. Reconcile the journal before restarting')
                if (not args.continuous and journal.committed() >= risk.max_total_cost
                        and args.exit_bid_above is None and args.stop_bid_at_or_below is None
                        and args.late_model_exit_probability_above is None and rolling_kelly_config is None):
                    print('Cumulative spending ceiling reached; no further orders will be submitted.', flush=True)
                    return 0
                started = time.monotonic()
                if observation_pool is not None:
                    observation_pool.check_health()
                    observation_pool.drain_output()
                settlement = None if settlement_worker is None else settlement_worker.poll(journal)
                if args.continuous and started >= next_settlement_check:
                    if settlement_worker is not None:
                        settlement_worker.start(journal)
                    else:
                        if args.continue_unresolved and journal.bounded_pending_ioc_ids():
                            try:
                                verify_settled_exposure(reader, journal, apply=True, max_pages=2, max_markets=1)
                            except EngineError:
                                pass
                        with journal.exclusive():
                            settlement = refresh_settled_allowances(reader, journal)
                    next_settlement_check = time.monotonic() + 15
                if settlement is not None:
                    emit_research(research, 'settlement_refresh', settlement)
                    if settlement['released_count']:
                        print(f"Confirmed settlement released $"+str(settlement['released_cost'])+
                              f"; active reserved $"+str(journal.active_committed())+'.', flush=True)
                visit_assets = active_assets[first:] + active_assets[:first]
                for asset_index, asset in enumerate(visit_assets):
                    if report_publisher is not None:
                        report_publisher.check_health()
                    asset_started = None if execution_metrics is None else time.monotonic()
                    if observation_pool is not None:
                        observation_pool.check_health()
                    if stop_path is not None and stop_path.exists():
                        return 0
                    should_stop = None if stop_path is None else lambda: stop_path.exists()
                    observation = None
                    exit_enabled = (args.exit_bid_above is not None or args.stop_bid_at_or_below is not None
                        or args.late_model_exit_probability_above is not None or rolling_kelly_config is not None)
                    execution = (None if not exit_enabled else executor.execute_exit(asset,
                        should_stop=should_stop))
                    initial_exit = execution
                    model_after_skip = ((args.late_model_exit_probability_above is not None or rolling_kelly_config is not None)
                        and isinstance(execution, dict) and execution.get('status') == 'skipped'
                        and execution.get('client_order_id') is None)
                    if execution is None or model_after_skip:
                        try:
                            observation = services[asset].sample()
                        except Exception:
                            if asset != 'GOLD' and args.late_model_exit_probability_above is None and rolling_kelly_config is None:
                                raise
                            observation = dict(kind='kalshi_minute_five_observation', version=1,
                                observation_only=True, strategy='minute_five', asset=asset, status='unavailable',
                                reason=('Gold feed or independent model data is unavailable'
                                    if args.late_model_exit_probability_above is None and rolling_kelly_config is None else
                                    'A fresh matched model forecast is unavailable'), fetched_at=time.time())
                        if stop_path is not None and stop_path.exists():
                            return 0
                        if model_after_skip:
                            # A protective skip may still need new model evidence,
                            # but must not fall through into an entry. Otherwise
                            # execute() owns the single post-sample model exit pass;
                            # repeating it here ages the same forecast twice.
                            execution = executor.execute_exit(asset, should_stop=should_stop,
                                model_snapshot=observation)
                            if execution is None:
                                execution = initial_exit
                        if execution is None:
                            execution = executor.execute(observation, should_stop=should_stop,
                                **(dict(price_exits_checked=True) if exit_enabled else {}))
                    else:
                        observation = observation or reports.get(asset, {}).get('observation') or dict(
                            kind='kalshi_minute_five_observation', version=1, observation_only=True,
                            strategy='minute_five', asset=asset, status='unavailable', fetched_at=time.time(),
                            reason='Price exit evaluated independently of the model forecast')
                    reports[asset] = dict(observation=observation, execution=execution)
                    if execution_metrics is not None:
                        execution_metrics.record(asset, observation, execution, started_at=asset_started,
                            completed_at=time.monotonic(), now=time.time())
                    slot_label = ''
                    if args.entry_schedule == 'every-minute' and execution.get('minutes_remaining') is not None:
                        slot_seconds = max(0, round(execution['minutes_remaining'] * 60))
                        slot_label = f' [{slot_seconds // 60}m {slot_seconds % 60:02d}s remaining]'
                    message = f"{asset}{slot_label}: {execution['status']} — {execution['reason']}"
                    if execution['status'] in ('filled', 'canceled') and all(
                            execution.get(name) is not None for name in ('fill_count', 'count', 'reserved_cost')):
                        if execution.get('action') == 'sell':
                            message += (f"; sold {execution['fill_count']}/{execution['count']} "
                                        f"{execution['held_outcome'].upper()} contracts at limit "
                                        f"${execution['exit_limit_bid']}; reserved ${execution['reserved_cost']}")
                        else:
                            side_label = execution.get('outcome', '').upper()
                            message += (f"; filled {execution['fill_count']}/{execution['count']} {side_label} contracts; "
                                        f"reserved ${execution['reserved_cost']}")
                            if execution.get('selected_probability') is not None:
                                probability = Decimal(str(execution['selected_probability'])) * 100
                                message += f'; selected-side model probability {probability:.2f}%'
                            if execution.get('net_position_after') is not None:
                                net = Decimal(execution['net_position_after'])
                                message += ('; net position flat' if net == 0 else
                                    f'; net position {abs(net):f} ' + ('YES' if net > 0 else 'NO'))
                        current_allowances = journal.allowance_snapshot(ticker=execution.get('ticker'))
                        message += (f"; {ceiling_label} remaining ${current_allowances['cumulative_remaining']}; "
                                    f"cash allowance remaining ${current_allowances['cash_allowance_remaining']}; "
                                    f"market remaining ${current_allowances['market_remaining']}")
                    message += _execution_quality_suffix(execution)
                    if message != previous.get(asset):
                        print(message, flush=True)
                        previous[asset] = message
                    # Normal rounds publish one full journal snapshot instead of
                    # rebuilding it for every asset. Uncertain outcomes and a
                    # journal halt still publish immediately before continuing
                    # or stopping the coordinator.
                    if report_path is not None and (asset_index == len(visit_assets) - 1
                            or execution.get('status') == 'unknown' or journal.halted()):
                        history = report_history.capture(journal)
                        journal_summary = history.summary
                        report_allowances = history.allowances_for(execution.get('ticker'))
                        report_payload = dict(kind='kalshi_minute_five_live_report', version=1,
                            mode='live', generated_at=time.time(), assets=list(assets),
                            report_session_id=report_session_id, environment='production',
                            report_history=report_history.snapshot(),
                            settlement_mode=args.settlement_mode if args.continuous else 'disabled',
                            **({} if settlement_worker is None else dict(settlement_maintenance=settlement_worker.snapshot())),
                            **({} if not entry_controls else dict(managed_assets=list(assets),
                                entry_assets=list(entry_assets), entry_contracts=args.entry_contracts,
                                entry_quantity_scope='new_entries_only', runtime_entry_controls=True)),
                            account_read_mode='parallel_paced' if account_reader is not None else 'sequential',
                            planning_read_mode=planning_read_mode, report_mode=args.report_mode,
                            **({} if discovery_cache is None else dict(discovery_cache=discovery_cache.snapshot())),
                            **({} if args.decision_model == 'monte-carlo' else dict(decision_model=args.decision_model)),
                            **_garch_report_fields(args, assets),
                            **({} if scheduled_policy_config is None else dict(
                                scheduled_policy=scheduled_policy_config.to_dict())),
                            **({} if uniform_pacing_config is None else dict(
                                uniform_pacing=uniform_pacing_config.to_dict(),
                                maximum_order_budget=budget_text(risk.max_order_cost))),
                            **({} if observation_pool is None else dict(parallel=observation_pool.snapshot())),
                            **({} if execution_metrics is None else dict(
                                execution_metrics=execution_metrics.snapshot(),
                                coordinator_mode=coordinator_mode, writer_count=1)),
                            **({} if activity is None else dict(activity_audit=dict(activity.status))),
                            **({} if research is None else dict(research_capture=research_health(research))),
                            **({} if not args.parallel_assets else dict(
                                read_coordination=_read_coordination_status(client, args))),
                            max_total_cost=str(risk.max_total_cost),
                            allowance_mode='continuous' if args.continuous else 'lifetime',
                            continue_unresolved=args.continue_unresolved,
                            **({} if rolling_kelly_config is None else dict(
                                rolling_kelly=rolling_kelly_config.to_dict(),
                                probability_margin_status='configured_sensitivity_not_calibrated')),
                            skip_probability_at_or_above=(None if args.skip_probability_at_or_above is None
                                else format(mode_settings['skip_probability_at_or_above'], 'f')),
                            exit_bid_above=(None if args.exit_bid_above is None
                                else format(mode_settings['exit_bid_above'], 'f')),
                            exit_seconds_before_close=None if args.exit_bid_above is None else args.exit_seconds_before_close,
                            **({} if args.stop_bid_at_or_below is None else dict(
                                stop_bid_at_or_below=format(mode_settings['stop_bid_at_or_below'], 'f'),
                                stop_seconds_before_close=args.stop_seconds_before_close)),
                            **({} if args.opposite_bid_at_or_below is None else dict(
                                opposite_bid_at_or_below=format(mode_settings['opposite_bid_at_or_below'], 'f'),
                                opposite_after_seconds=args.opposite_after_seconds)),
                            **({} if args.late_model_exit_probability_above is None else dict(
                                late_model_exit_probability_above=format(mode_settings['late_model_exit_probability_above'], 'f'),
                                late_model_exit_seconds_before_close=args.late_model_exit_seconds_before_close)),
                            **({} if args.eth_late_model_direction == 'first-fresh' else dict(
                                eth_late_model_direction=args.eth_late_model_direction)),
                            **({} if not args.resume_after_exit else dict(
                                resume_after_exit=True, exit_reentry_cutoff_seconds=60)),
                            **({} if regime_config is None else dict(regime_switching=regime_config.to_dict())),
                            sizing_profile=args.sizing_profile,
                            gaussian_sizing=profile.to_dict() if isinstance(profile, GaussianSizing) else None,
                            **({} if not isinstance(profile, UniformSizing) else dict(uniform_sizing=profile.to_dict())),
                            bet_budget=budget_text(bet_budget) if profile is None else None,
                            initial_bet_budget=budget_text(initial_budget) if profile is None else None,
                            early_bet_budget=None if early_budget is None else budget_text(early_budget),
                            entry_schedule=args.entry_schedule,
                            schedule=None if args.entry_schedule == 'once' else dict(
                                schedule.to_dict(), market_budget=budget_text(risk.max_market_cost)),
                            history_seconds=config.max_history_seconds,
                            reserved_cost=journal_summary['committed_cost'],
                            halted=journal_summary['halted'], allowances=report_allowances, markets=reports)
                        urgent = execution.get('status') == 'unknown' or report_payload['halted']
                        prepared = PreparedReport(report_payload, history, urgent=urgent)
                        if report_publisher is None:
                            _write_report(report_path, prepared.compose())
                        elif urgent:
                            report_publisher.publish_urgent(prepared)
                        else:
                            report_publisher.submit(prepared)
                    if journal.halted():
                        raise CLIError('Order outcome is uncertain; submissions halted. Inspect the order and reconcile the journal')
                if activity is not None:
                    activity.poll()
                if research is not None:
                    health = research_health(research)
                    loss = tuple(health.get(name, 0) for name in ('dropped_count', 'rejected_count',
                        'write_error_count', 'payload_omission_count', 'disabled', 'health_unavailable'))
                    if any(loss) and loss != previous_research_loss:
                        print('Research capture is incomplete; inspect research_capture health and local storage. '
                            'Trading risk and order policies remain unchanged.', file=sys.stderr, flush=True)
                    previous_research_loss = loss
                first = (first + 1) % len(active_assets)
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('Live runner stopped. Filled positions remain open for official settlement.', flush=True)
        return 130
    except Exception as error:
        message = str(error) if isinstance(error, (CLIError, KalshiError, EngineError, EntryScheduleError,
            MinuteFiveError, FittedParameterError, CalibrationModelError, ShadowError,
            TimeSizingError, RegimeError, ReportPublicationError)) else 'Configuration, account data or local state is unavailable'
        print('Minute-five live runner stopped: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
