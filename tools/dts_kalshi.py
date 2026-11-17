"""Checkout-backed strategy commands; catalog inspection has no side effects."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
from types import MappingProxyType


class ConsoleError(ValueError):
    """Fixed command-selection errors, before a strategy can start."""


@dataclass(frozen=True)
class Command:
    name: str
    title: str
    module: str
    category: str
    capability: str
    prefix: tuple[str, ...] = ()
    pins: tuple[tuple[str, object], ...] = ()
    journal_name: str | None = None

    @property
    def console(self):
        return 'dts-kalshi-' + self.name


_COMMANDS = (
    Command('scheduled', 'Scheduled Monte Carlo median entries', 'kalshi_minute_five_live',
        'strategy', 'Account preflight; live needs --run --allow-live', journal_name='console-scheduled-live-v1.sqlite3'),
    Command('regime', 'Crypto regime-switching profile', 'kalshi_minute_five_live',
        'strategy', 'BTC/ETH/SOL/XRP; shared journal, explicit live arming and existing regime requirements',
        pins=(('--regime-switching', True),), journal_name='console-regime-live-v1.sqlite3'),
    Command('value', 'Directional executable value / aggregate Kelly', 'kalshi_value_live',
        'strategy', 'One BTC/ETH/SOL/XRP asset per exclusive journal; paper by default; qualified calibration for live'),
    Command('neutral', 'Actual-data distinct-strike neutral spread', 'kalshi_neutral_live',
        'strategy', 'Paper by default; experimental live requires original explicit flags'),
    Command('ensemble', 'Ensemble convergence', 'kalshi_paper_strategy',
        'strategy', 'Actual-data paper only; --run starts observations', pins=(('--strategy', 'ensemble'),)),
    Command('rolling', 'Rolling model-versus-market residual', 'kalshi_paper_strategy',
        'strategy', 'Actual-data paper only; --run starts observations', pins=(('--strategy', 'rolling'),)),
    Command('rolling-rsi', 'Rolling residual with RSI confirmation', 'kalshi_paper_strategy',
        'strategy', 'Actual-data paper only; --run starts observations', pins=(('--strategy', 'rolling-rsi'),)),
    Command('minute-five-paper', 'Once-per-window minute-five median', 'kalshi_paper_strategy',
        'strategy', 'Fixed $1 paper entries; --run starts observations', pins=(('--strategy', 'minute-five'),)),
    Command('neutral-research', 'Synthetic / snapshot neutral spread research', 'kalshi_neutral_paper',
        'research', 'Paper only; source is explicitly --demo, --snapshot or --replay'),
    Command('shared-paper', 'Shared neutral paper coordinator', 'kalshi_portfolio_paper',
        'research', 'Simulated shared capital; --public reads actual data'),
    Command('basket-scan', 'Model-free threshold basket scanner', 'kalshi_basket_scan',
        'research', '--live means public quotes; no order capability'),
    Command('basket-replay', 'Threshold basket execution replay', 'kalshi_basket_execution_replay',
        'research', 'Offline hypothetical fills only'),
    Command('rolling-replay', 'Baseline / RSI chronological comparison', 'kalshi_rolling_replay',
        'research', 'Offline recorded-evidence replay'),
    Command('value-replay', 'Directional value chronological replay', 'kalshi_value_replay',
        'research', 'Offline replay'),
    Command('value-calibrate', 'Value-model probability calibration', 'kalshi_value_calibrate',
        'research', 'Calibration utility; does not start execution'),
    Command('ensemble-experiment', 'Ensemble archive, grading and scoring tools', 'kalshi_algorithm',
        'research', 'Original run-paper / grade / score subcommands'),
    Command('forecast-shadow', 'Settlement-aware forecast capture and scoring', 'kalshi_shadow',
        'utility', 'Read-only forecast evidence; record / grade / score'),
    Command('portfolio-shadow', 'Hypothetical shared reservation tests', 'kalshi_portfolio_shadow',
        'utility', 'Synthetic or supplied-file shadow admission; never orders'),
    Command('portfolio', 'Asset discovery, feeds and portfolio wrapper', 'kalshi_portfolio',
        'utility', 'Data commands are read-only; run delegates explicit legacy execution'),
    Command('assess', 'Portfolio payoff and account assessment', 'kalshi_portfolio_assess',
        'utility', 'Read-only / offline assessment'),
    Command('guard', 'Pure shared-capital admission assessment', 'kalshi_portfolio_guard',
        'utility', 'Assessment only; no live reservation authority'),
    Command('dashboard', 'Local monitoring and manual confirmations', 'kalshi_live',
        'utility', 'Paper by default; original explicit flags for account/order features'),
    Command('history', 'Historical download and model-fit tools', 'kalshi_history',
        'utility', 'Original history subcommands'),
    Command('commodities', 'Commodity onboarding and research', 'kalshi_commodities',
        'utility', 'Original onboarding / analysis subcommands'),
    Command('latency', 'Latency capture and execution-friction replay', 'kalshi_latency',
        'research', 'Capture / replay only; no order capability'),
    Command('crypto-readiness', 'Sustained crypto REST history and latency', 'kalshi_crypto_readiness',
        'utility', 'Offline configuration by default; --run collects read-only data without account or order access'),
    Command('research', 'Recorded requests, decisions and strategy diagnostics', 'kalshi_research',
        'research', 'Read-only local research analysis and sanitized JSONL export; no venue access'),
    Command('research-outcomes', 'Recorded-contract outcome labels', 'kalshi_research_outcomes',
        'research', 'Local preview by default; --fetch reads public markets only and appends research evidence'),
    Command('reconcile', 'Existing order reconciliation', 'kalshi_reconcile',
        'utility', 'Read-only by default; --apply explicitly updates its existing journal'),
    Command('worker', 'Generic supplied-signal worker', 'kalshi_bot',
        'framework', 'Original paper/demo/live flags; not a forecast algorithm'),
)
COMMANDS = MappingProxyType({command.name: command for command in _COMMANDS})


def _tools_dir():
    tools = Path(__file__).resolve().parent
    if not (tools.joinpath('kalshi_bot.py').is_file() and tools.joinpath('kalshi', '__init__.py').is_file()):
        raise ConsoleError('These source-checkout commands require an editable install: python3 -m pip install -e /path/to/Derivative_Trading_Strategy')
    return tools


def catalog():
    return dict(kind='dts_kalshi_commands', version=1, execution_started=False,
        commands=[dict(name=row.name, console=row.console, title=row.title, category=row.category,
            capability=row.capability, original='python3 tools/' + row.module + '.py',
            pinned=dict(row.pins), default_journal=row.journal_name) for row in _COMMANDS])


def _show_catalog():
    print('Kalshi strategy commands\n')
    for category in ('strategy', 'research', 'utility', 'framework'):
        print(category.capitalize() + ':')
        for row in _COMMANDS:
            if row.category == category:
                print(f'  {row.console:<34} {row.title}')
        print()
    print('Use COMMAND --help for its original flags, or dts-kalshi NAME ... .')
    print('No-argument commands show help. Original scripts and live arming requirements remain available.')


def _target_parser(module):
    for name in ('parser', '_parser'):
        factory = getattr(module, name, None)
        if callable(factory):
            return factory()
    # Some existing utilities build argparse inside main(). They need no
    # profile selection or default overrides and can retain their own parsing.
    return None


def _long_options(parser):
    options = set()
    for action in parser._actions:
        options.update(option for option in action.option_strings if option.startswith('--'))
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                options.update(_long_options(child))
    return options


def _normalize_options(argv, parser):
    if parser is None:
        return list(argv)
    options = _long_options(parser)
    result = []
    positional = False
    for token in argv:
        if positional or not token.startswith('--') or token == '--':
            positional = positional or token == '--'
            result.append(token)
            continue
        name, separator, value = token.partition('=')
        if name in options:
            matches = [name]
        else:
            matches = [option for option in options if option.startswith(name)]
        if len(matches) > 1:
            raise ConsoleError('Use complete, unambiguous option names in the strategy console')
        result.append((matches[0] if matches else name) + (separator + value if separator else ''))
    return result


def _pin(args, option, expected):
    result, index = [], 0
    while index < len(args):
        token = args[index]
        if token == '--':
            result.extend(args[index:])
            break
        key, separator, value = token.partition('=')
        if key != option:
            result.append(token)
        elif expected is True:
            if separator:
                raise ConsoleError('The selected strategy fixes ' + option + ' as an enabled flag')
        else:
            if not separator:
                index += 1
                value = args[index] if index < len(args) else None
            if value != expected:
                raise ConsoleError('The selected strategy fixes ' + option + '=' + str(expected))
        index += 1
    prefix = [option] if expected is True else [option, str(expected)]
    return prefix + result


def dispatch(name, argv):
    """Select one existing engine; never add run, live or policy-update flags."""
    original_argv, original_path = sys.argv[:], sys.path[:]
    try:
        if name not in COMMANDS:
            raise ConsoleError('Unknown command; run dts-kalshi --list')
        command = COMMANDS[name]
        tools = _tools_dir()
        source = tools / (command.module + '.py')
        if not source.is_file():
            raise ConsoleError('The selected strategy launcher is missing from this checkout')
        sys.path.insert(0, str(tools))
        module = importlib.import_module(command.module)
        if Path(getattr(module, '__file__', '')).resolve() != source.resolve():
            raise ConsoleError('The selected launcher resolves outside this source checkout')
        parser = _target_parser(module)
        if parser is None and (command.pins or command.journal_name):
            raise ConsoleError('The selected strategy requires an inspectable argument parser for its fixed profile')
        args = _normalize_options(list(argv) or ['--help'], parser)
        if name == 'scheduled' and any(token.split('=', 1)[0].startswith('--regime') for token in args):
            raise ConsoleError('Regime settings require dts-kalshi-regime; the scheduled command keeps regime switching disabled')
        for option, expected in command.pins:
            args = _pin(args, option, expected)
        if command.journal_name and not any(token.split('=', 1)[0] == '--journal' for token in args):
            args.extend(('--journal', str(Path.home() / '.local' / 'state' / 'dts-kalshi' / command.journal_name)))
        args = list(command.prefix) + args
        sys.argv = [command.console, *args]
        if '--help' in args or '-h' in args:
            print(command.title + '\n' + command.capability)
            if command.pins:
                print('Profile selection: ' + ', '.join(option + (' enabled' if value is True else '=' + str(value))
                    for option, value in command.pins))
            if command.journal_name:
                print('Default journal: ' + command.journal_name + '; explicit --journal preserves your selected existing path')
            print()
        result = module.main(args)
        return 0 if result is None else int(result)
    except ConsoleError as error:
        print('Kalshi console: ' + str(error), file=sys.stderr)
        return 2
    except ModuleNotFoundError:
        print('Kalshi console: selected launcher dependencies are unavailable. Install requirements-kalshi.txt and check the selected source launcher.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Kalshi console stopped.', file=sys.stderr)
        return 130
    finally:
        sys.argv, sys.path[:] = original_argv, original_path


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args in (['--help'], ['-h'], ['--list']):
        _show_catalog()
        return 0
    if args == ['--json']:
        print(json.dumps(catalog(), indent=2))
        return 0
    return dispatch(args[0], args[1:])


def _alias(name):
    def entry(argv=None):
        return dispatch(name, list(sys.argv[1:] if argv is None else argv))
    entry.__name__ = name.replace('-', '_') + '_main'
    return entry


for _command in _COMMANDS:
    globals()[_command.name.replace('-', '_') + '_main'] = _alias(_command.name)


if __name__ == '__main__':
    raise SystemExit(main())
