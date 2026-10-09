#!/usr/bin/env python3
"""Verify a backup and restore to a NEW data directory. Never overwrite existing data."""
from __future__ import annotations
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import re
import sys

APPLICATION_ID=1146377044

def verify_depth_schema(source, version):
    """Verify supported depth/backfill definitions without changing the backup."""
    if version < 6:
        return
    include = Path(__file__).resolve().parents[1] / 'src/backend/cpp_src/storage/include/dts'
    definitions = re.findall(r'R"SQL\((.*?)\)SQL"', (include / 'depth_schema.hpp').read_text(), re.S)
    if len(definitions) != 2:
        raise ValueError('Known depth schema definitions are unavailable')
    query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ('depth_sessions','depth_events') ORDER BY type,name"
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.executescript(definitions[0])
        if version == 8:
            expected.executescript(definitions[1])
        if source.execute(query).fetchall() != expected.execute(query).fetchall():
            raise ValueError('Unrecognized depth schema; backup was not restored')
        if source.execute("SELECT count(*) FROM sqlite_master WHERE name='depth_sessions_v8'").fetchone()[0]:
            raise ValueError('Unrecognized temporary depth table')
        backfill_query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ('history_backfills','history_backfill_windows') ORDER BY type,name"
        actual_backfill = source.execute(backfill_query).fetchall()
        if version == 6 and actual_backfill:
            raise ValueError('Unrecognized depth/backfill schema combination')
        if version == 7 or actual_backfill:
            ddl = re.findall(r'R"SQL\((.*?)\)SQL"', (include / 'backfill_schema.hpp').read_text(), re.S)[0]
            expected.executescript(ddl)
            if actual_backfill != expected.execute(backfill_query).fetchall():
                raise ValueError('Unrecognized backfill schema; backup was not restored')


def restore(backup: Path, data_dir: Path) -> Path:
    backup=backup.expanduser().resolve(strict=True)
    data_dir=data_dir.expanduser().absolute()
    if not backup.is_file():raise ValueError('Backup must be a regular SQLite file')
    if data_dir.exists():raise ValueError('Destination already exists. Choose a NEW directory; no file was overwritten')
    if not data_dir.parent.is_dir():raise ValueError('Destination parent directory must already exist')
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as source:
        version=source.execute('PRAGMA user_version').fetchone()[0]
        if source.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID or version not in (1,2,3,4,5,6,7,8):
            raise ValueError('Not a supported Derivative Lab time-series backup')
        verify_depth_schema(source,version)
        if source.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Backup integrity check failed')
        if source.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Backup foreign-key check failed')
        data_dir.mkdir(mode=0o700,exist_ok=False)
        final=data_dir/'timeseries.sqlite3'
        temp=data_dir/'restore.sqlite.partial'
        # The directory is private before the destination file is created.
        fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        try:
            with closing(sqlite3.connect(temp)) as destination:
                source.backup(destination)
                destination.execute('PRAGMA journal_mode=DELETE')
                if destination.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Restored data failed integrity validation')
            fd=os.open(temp,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            temp.rename(final)
            fd=os.open(data_dir,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except Exception:
            # Keep only an explicitly incomplete file; never delete original data.
            raise
    return final

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup',required=True,type=Path)
    p.add_argument('--data-dir',required=True,type=Path)
    args=p.parse_args(argv)
    try:
        path=restore(args.backup,args.data_dir)
        print(f'Restored and verified: {path}\nStart the server with DTS_DATA_DIR={path.parent}')
        return 0
    except (OSError,ValueError,sqlite3.Error) as error:
        print(f'Restore stopped: {error}',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
