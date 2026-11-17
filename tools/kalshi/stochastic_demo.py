"""Explicitly synthetic feature-level examples for offline comparison plumbing.

These are not CF observations, venue outcomes, fitted evidence or executable
books. Every contract has one shared target across its forecast horizons.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import random

from .crypto_assets import asset_spec


def demo_rows(assets=('BTC', 'ETH', 'SOL', 'XRP'), *, days=14, contracts_per_day=12, seed=7):
    if (type(days) is not int or not 7 <= days <= 60
            or type(contracts_per_day) is not int or not 1 <= contracts_per_day <= 48
            or type(seed) is not int or not 0 <= seed < 2**32):
        raise ValueError('Invalid synthetic study size or seed')
    if (not isinstance(assets, (tuple, list)) or not assets
            or len(set(assets)) != len(assets)):
        raise ValueError('Select each synthetic asset once')
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    bases = dict(BTC=100000.0, ETH=3000.0, SOL=150.0, XRP=2.0)
    rows = []
    for asset in assets:
        profile = asset_spec(asset)
        for day in range(days):
            for index in range(contracts_per_day):
                closed = start + day * 86400 + 3600 + index * 900
                nominal = bases[asset]
                sigma = .00012
                context = rng.uniform(-2, 2)
                inputs = [1.0, context, context * .7 + rng.gauss(0, .3),
                          context * .3 + rng.gauss(0, .4), rng.gauss(0, .5), rng.uniform(-.5, .5)]
                fast = sigma * math.exp(inputs[-1] / 2)
                # A feature-level joint toy, not a claimed physical CF process.
                # Inputs are generated before its one common settlement target.
                actual = nominal + nominal * sigma * rng.gauss(0, 18)
                y = int(actual >= nominal)
                ticker = profile.series + f'-SYNTHETIC-{day:03d}-{index:03d}'
                for horizon in (900, 300, 60):
                    as_of = closed - horizon
                    offsets = [float(horizon - offset) for offset in range(59, -1, -1)]
                    variance_time = math.fsum((2 * (60 - i) - 1) * value
                                              for i, value in enumerate(offsets))
                    loading = math.fsum(offsets) / 60
                    noise_scale = nominal * sigma * math.sqrt(variance_time) / 60
                    drift = nominal * sigma / math.sqrt(1800) * .25 * context
                    spot = actual - drift * loading + rng.gauss(0, 1) * noise_scale
                    rows.append(dict(kind='kalshi_stochastic_feature_row', version=1,
                        asset=asset, ticker=ticker, source=profile.source, as_of=as_of,
                        model_time=as_of, close_time=closed,
                        day=datetime.fromtimestamp(closed, timezone.utc).strftime('%Y-%m-%d'),
                        spot=spot, known_sum=0.0, future_offsets=offsets,
                        baseline_sigma=sigma, fast_sigma=fast, slow_sigma=sigma,
                        history_seconds=1800, features=list(inputs),
                        target_price=str(nominal), strike_type='greater_or_equal',
                        payoff=None, payoff_status='unverified_legacy_approximation', market=None,
                        y=y, realized_average=actual, label_available_at=closed + 1,
                        target_available_at=closed + 1, target_kind='synthetic_average',
                        market_probability_yes=None, provenance='synthetic_demo', synthetic=True))
    rows.sort(key=lambda row: (row['as_of'], row['asset'], row['ticker']))
    return dict(rows=rows,
        coverage={asset: dict(status='synthetic', contracts=days * contracts_per_day,
            feature_rows=days * contracts_per_day * 3) for asset in assets},
        provenance=dict(synthetic=True, source='feature_level_demo',
            official_labels=False, observed_index_history=False, executable_books=False,
            historical_receive_latency_known=False,
            limitation='Synthetic plumbing only; not evidence of predictive quality or profitability'))
