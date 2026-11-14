#!/usr/bin/env python3
"""Local Bitcoin 15-minute market view, Kelly preview and user-confirmed orders.

Defaults to a paper-only console on http://127.0.0.1:8091/. Signing keys stay in
the server process. Merely opening this console never submits an order.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _risk, _risk_arguments


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--port', type=int, default=8091)
    result.add_argument('--journal', type=Path, required=True)
    result.add_argument('--algorithm-report', type=Path,
                        help='Monitor an ensemble paper-runner JSON report without enabling automatic exchange orders')
    result.add_argument('--probability-fit', type=Path,
                        help='Load a historical fit for its matching asset in the Monte Carlo and Greeks monitors')
    result.add_argument('--mode', choices=('paper', 'demo', 'live'), default='paper')
    result.add_argument('--environment', choices=('demo', 'production'), default='production')
    result.add_argument('--allow-orders', action='store_true', help='Enable explicit demo/live confirmations')
    result.add_argument('--allow-live', action='store_true', help='Additionally enable production order confirmations')
    result.add_argument('--account-view', action='store_true',
                        help='Read account positions with local credentials, including while execution stays paper-only')
    result.add_argument('--timeout', type=float, default=3, help='Per-read transport timeout in seconds, at most 10')
    _risk_arguments(result)
    result.set_defaults(sizing_mode='kelly', required_series='KXBTC15M', fee_reserve_per_contract='0.03')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    client = account_client = service = server = None
    try:
        from kalshi.client import KalshiClient, KalshiError
        from kalshi.live import LiveService, create_server
        from kalshi.fitted_parameters import FittedParameterError, load_fitted_parameters
        if not 1024 <= args.port <= 65535:
            raise CLIError('Local console port must be between 1024 and 65535')
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
            raise CLIError('Transport timeout must be positive and at most 10 seconds')
        if args.sizing_mode != 'kelly' or args.required_series != 'KXBTC15M':
            raise CLIError('The live console requires Bitcoin 15-minute Kelly mode')
        if args.mode == 'paper' and (args.allow_orders or args.allow_live):
            raise CLIError('Paper mode cannot enable exchange orders')
        if args.mode == 'demo' and args.environment != 'demo':
            raise CLIError('Demo orders require the demo environment')
        if args.mode == 'live' and args.environment != 'production':
            raise CLIError('Live orders require the production environment')
        if args.mode != 'paper' and not args.allow_orders:
            raise CLIError('Demo/live order confirmations require --allow-orders')
        if args.mode == 'live' and not args.allow_live:
            raise CLIError('Production order confirmations additionally require --allow-live')
        if args.mode == 'demo' and args.allow_live:
            raise CLIError('Demo mode cannot enable production orders')

        journal_path = _private_path(args.journal)
        risk = _risk(args)
        probability_fit = None
        if args.probability_fit is not None:
            if args.environment != 'production':
                raise CLIError('Historical CF probability fits require production index data')
            probability_fit = load_fitted_parameters(_private_path(args.probability_fit))
        credentials = {} if args.mode == 'paper' and not args.account_view else {
            'key_id': os.environ.get('KALSHI_API_KEY_ID'),
            'private_key_path': os.environ.get('KALSHI_PRIVATE_KEY_PATH'),
        }
        if (args.mode != 'paper' or args.account_view) and not all(credentials.values()):
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally')
        client = KalshiClient(environment=args.environment, allow_writes=args.mode != 'paper',
                              timeout=args.timeout, **(credentials if args.mode != 'paper' else {}))
        if args.mode == 'paper' and args.account_view:
            account_client = KalshiClient(environment=args.environment, allow_writes=False,
                                          timeout=args.timeout, **credentials)
        extra = {'account_client': account_client} if account_client is not None else {}
        if args.algorithm_report is not None:
            extra['algorithm_report'] = _private_path(args.algorithm_report)
        if probability_fit is not None:
            extra['probability_fit'] = probability_fit
        service = LiveService(client=client, journal_path=journal_path, risk=risk,
                              mode=args.mode, environment=args.environment,
                              orders_enabled=args.mode != 'paper', **extra)
        server = create_server(service, port=args.port)
        print(f'Kalshi console: http://127.0.0.1:{args.port}/', flush=True)
        print(f'Mode: {args.mode}; data: {args.environment}; every order needs a fresh explicit confirmation.', flush=True)
        if args.account_view:
            print('Account positions: authenticated read-only view; execution mode is unchanged.', flush=True)
        if probability_fit is not None:
            print(f'Historical probability fit loaded: {probability_fit.asset}; '
                  f'EWMA half-life {probability_fit.half_life_seconds:g}s; '
                  f'volatility scale {probability_fit.volatility_scale:.6f}.', flush=True)
        print('Keep this process running. Ctrl-C closes the console; no account liquidation is performed.', flush=True)
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from kalshi.client import KalshiError
        from kalshi.fitted_parameters import FittedParameterError
        message = str(error) if isinstance(error, (CLIError, KalshiError, FittedParameterError)) else 'Console configuration, dependency, port or journal is unavailable'
        print('Kalshi console stopped: ' + message, file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.close()
        elif client is not None:
            client.close()
        if service is None and account_client is not None:
            account_client.close()


if __name__ == '__main__':
    raise SystemExit(main())
