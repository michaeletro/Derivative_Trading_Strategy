#!/usr/bin/env python3
"""Verify a backup and restore to a NEW data directory. Never overwrite existing data."""
from __future__ import annotations
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

APPLICATION_ID=1146377044

def restore(backup: Path, data_dir: Path) -> Path:
    backup=backup.expanduser().resolve(strict=True)
    data_dir=data_dir.expanduser().absolute()
    if not backup.is_file():raise ValueError('Backup must be a regular SQLite file')
    if data_dir.exists():raise ValueError('Destination already exists. Choose a NEW directory; no file was overwritten')
    if not data_dir.parent.is_dir():raise ValueError('Destination parent directory must already exist')
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as source:
        if source.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID or source.execute('PRAGMA user_version').fetchone()[0] not in (1,2,3):
            raise ValueError('Not a supported Derivative Lab time-series backup')
        if source.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Backup integrity check failed')
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
