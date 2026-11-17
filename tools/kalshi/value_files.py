"""Bounded JSON destinations that cannot overwrite unrelated strategy state."""
from __future__ import annotations

import json
from pathlib import Path


def json_output_path(path, expected_kind, *, inputs=(), maximum_bytes=100000000):
    from kalshi_bot import _private_path, CLIError
    target = _private_path(path)
    protected = set()
    for source in inputs:
        if source is None:
            continue
        source = Path(source).expanduser().absolute()
        for suffix in ('', '.lock', '.meta.json', '-wal', '-shm', '-journal'):
            protected.add(Path(str(source) + suffix).resolve())
    if target.resolve() in protected:
        raise CLIError('JSON output must use a separate path from inputs and their sidecars')
    if target.exists():
        try:
            if not target.is_file() or target.stat().st_size > maximum_bytes:
                raise ValueError()
            with target.open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    raise ValueError()
            value = json.loads(target.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('kind') != expected_kind
                    or type(value.get('version')) is not int or value['version'] != 1):
                raise ValueError()
        except (OSError, UnicodeError, ValueError, TypeError):
            raise CLIError('Existing output is unrelated state; choose a separate JSON output path') from None
    return target
