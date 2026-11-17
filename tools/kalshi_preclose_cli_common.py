"""Bounded JSON and protected paths shared by the offline preclose commands."""
from __future__ import annotations

import json
import math
from pathlib import Path

from kalshi_bot import CLIError, _private_path


def read_json(path, *, limit=128_000_000):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CLIError('Preclose JSON contains duplicate keys')
            result[key] = value
        return result
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise CLIError('Preclose JSON must be finite')
        return number
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise CLIError('Preclose JSON exceeds the declared input bound')
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Preclose JSON must be finite')))
    except (json.JSONDecodeError, RecursionError):
        raise CLIError('Preclose input must be bounded valid JSON') from None


def binding(value, *, assets=('BTC', 'ETH')):
    asset, separator, location = value.partition('=')
    if not separator or asset not in assets or not location:
        raise CLIError('Use an explicit supported ASSET=PATH binding')
    path = _private_path(Path(location))
    if not path.is_file():
        raise CLIError('Each bound preclose input must already exist')
    return asset, path


def existing_input(path):
    selected = _private_path(Path(path))
    if not selected.is_file():
        raise CLIError('Each selected preclose input must already exist')
    return selected


def output_plan(outputs, inputs):
    """Validate every destination before writing any member of the output set."""
    selected = {name: _private_path(Path(path)) for name, (path, _) in outputs.items()}
    if len(set(selected.values())) != len(selected):
        raise CLIError('Preclose outputs require separate paths')
    reserved = [Path(str(path) + suffix) for path in inputs
                for suffix in ('', '-wal', '-shm', '-journal')]
    for name, path in selected.items():
        if path.suffix.lower() != '.json':
            raise CLIError('Use JSON destinations for preclose reports and artifacts')
        if any(path == source or path in source.parents or source in path.parents for source in reserved):
            raise CLIError('Preclose outputs cannot overlap inputs or SQLite sidecars')
        if any(path != other and (path in other.parents or other in path.parents)
               for other in selected.values()):
            raise CLIError('Preclose output paths cannot overlap each other')
        if path.exists():
            previous = read_json(path, limit=2_000_000)
            expected = outputs[name][1]
            if (not isinstance(previous, dict) or previous.get('kind') != expected
                    or type(previous.get('version')) is not int or previous['version'] != 1
                    or previous.get('execution_eligible') is not False):
                raise CLIError('Choose a new output path instead of replacing unrelated evidence')
    return selected
