#!/usr/bin/env python3
"""Reconfigure, build and launch this checkout. Never downloads SDKs or changes Git."""
from __future__ import annotations
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
def options(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['research','tws'],default='research')
    p.add_argument('--port',type=int,default=8081)
    p.add_argument('--build-dir',type=Path,default=ROOT/'build-workspace')
    p.add_argument('--jobs',type=int,default=2)
    p.add_argument('--sdk-root',type=Path,default=Path.home()/'.local/share/ibkr-api-10.45.01/IBJts')
    p.add_argument('--data-dir',type=Path,help='Persistent local recording directory, independent of checkout')
    p.add_argument('--backup-dir',type=Path,help='Directory for consistent SQLite backups')
    p.add_argument('--dry-run',action='store_true',help='Validate configuration and print non-secret actions only')
    return p.parse_args(argv)

def prepare(args):
    if not 1024<=args.port<=65535:raise ValueError('HTTP port must be between 1024 and 65535')
    if not 1<=args.jobs<=32:raise ValueError('Build jobs must be between 1 and 32')
    build=args.build_dir.expanduser().resolve()
    if build==ROOT:raise ValueError('Use a separate build directory, not the source root')
    cache=build/'CMakeCache.txt'
    if cache.exists():
        for line in cache.read_text().splitlines():
            if line.startswith('CMAKE_HOME_DIRECTORY:INTERNAL=') and Path(line.split('=',1)[1]).resolve()!=ROOT:
                raise ValueError('This build directory belongs to another checkout; choose --build-dir with a different path')
    env=dict(os.environ,HTTP_PORT=str(args.port),DTS_BROKER='tws' if args.mode=='tws' else 'none',ENABLE_IB_WS='false')
    cmd=['cmake','-S',str(ROOT),'-B',str(build),'-DCMAKE_BUILD_TYPE=Release','-DBUILD_TESTING=ON','-DDTS_BUILD_SERVER=ON',f'-DDTS_WITH_IBKR={"ON" if args.mode=="tws" else "OFF"}']
    if args.mode=='tws':
        sdk=args.sdk_root.expanduser().resolve()
        if not (sdk/'source/cppclient/client/EClientSocket.h').is_file():raise ValueError('IBKR SDK not found. Supply the extracted IBJts directory using --sdk-root')
        if len(env.get('DTS_API_TOKEN',''))<24:raise ValueError('TWS mode requires an exported DTS_API_TOKEN of at least 24 characters; it is not an IBKR password')
        cmd.append(f'-DIBKR_API_ROOT={sdk}')
    xdg=env.get('XDG_DATA_HOME','')
    default_data=(Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home()/'.local/share')/'derivative-lab'
    data=args.data_dir or Path(env.get('DTS_DATA_DIR') or default_data)
    backup=args.backup_dir or Path(env.get('DTS_BACKUP_DIR') or data/'backups')
    for path in (data,backup):
        if not path.expanduser().is_absolute():raise ValueError('Data/backup paths must be absolute')
    env['DTS_DATA_DIR']=str(data.expanduser().resolve())
    env['DTS_BACKUP_DIR']=str(backup.expanduser().resolve())
    return build,cmd,env

def main(argv=None):
    try:
        args=options(argv);build,configure,env=prepare(args)
        if shutil.which('cmake') is None:raise ValueError('CMake is not installed; see docs/greeks-scenarios.md')
        revision='unavailable (source archive)'
        if (ROOT/'.git').exists() and shutil.which('git'):
            r=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,text=True,capture_output=True,check=False)
            if re.fullmatch(r'[a-f0-9]{40}\n?',r.stdout):revision=r.stdout.strip()[:12]
        print(f'Source checkout: {ROOT}\nSource revision: {revision}\nBuild directory: {build}\nMode: {args.mode} (no order capability)',flush=True)
        print(f"Persistent database: {env['DTS_DATA_DIR']}/timeseries.sqlite3\nBackup directory: {env['DTS_BACKUP_DIR']}",flush=True)
        print('Local token: configured (not displayed)' if env.get('DTS_API_TOKEN') else 'Local token: none; unlock with an empty field in research mode',flush=True)
        if args.dry_run:
            print('Would reconfigure, build, test, then execute the server. No build or server commands were run.');return 0
        recorder=Path(env['DTS_DATA_DIR'])/'recorder.lock'
        if recorder.exists():
            fd=os.open(recorder,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError as e:raise ValueError('Recording database already in use. Stop the owning server or choose a separate --data-dir') from e
            finally:os.close(fd)
        try:
            with socket.socket() as sock:sock.bind(('127.0.0.1',args.port))
        except OSError as e:raise ValueError(f'Port {args.port} is unavailable. Stop the intended old server or select another --port; no process was killed') from e
        for cmd in [configure,['cmake','--build',str(build),'--parallel',str(args.jobs)],['ctest','--test-dir',str(build),'--output-on-failure']]:
            subprocess.run(cmd,cwd=ROOT,check=True)
        binary=build/'server'
        print(f'Open http://127.0.0.1:{args.port}/#sensitivities\nKeep this terminal running; Ctrl+C drains recording and creates a backup; wait for shutdown to finish.',flush=True)
        os.execve(binary,[str(binary)],env)
    except (ValueError,OSError,subprocess.CalledProcessError) as e:
        print(f'Launch stopped: {e}',file=sys.stderr);return 2
    except KeyboardInterrupt:
        return 130
if __name__=='__main__':raise SystemExit(main())
