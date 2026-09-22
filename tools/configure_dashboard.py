#!/usr/bin/env python3
"""Explicit one-time private profile setup; no builds or broker connections."""
import argparse
import json
from pathlib import Path
import sys
from local_config import initialize, load

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',required=True)
    p.add_argument('--mode',choices=['research','tws'],default='research')
    p.add_argument('--port',type=int,default=8081)
    p.add_argument('--ib-host',default='127.0.0.1')
    p.add_argument('--ib-port',type=int,default=7497)
    p.add_argument('--client-id',type=int,default=17)
    p.add_argument('--data-dir',type=Path,default=Path.home()/'.local/share/derivative-lab')
    p.add_argument('--sdk-root',type=Path,default=Path.home()/'.local/share/ibkr-api-10.45.01/IBJts')
    p.add_argument('--show-token',action='store_true',help='Explicitly reveal saved local token only to an interactive terminal')
    a=p.parse_args()
    try:
        if a.show_token:
            if not sys.stdout.isatty(): raise ValueError('Token display requires an interactive terminal; do not redirect to logs')
            _,token=load(a.profile);print('Local dashboard token (not an IBKR password):\n'+token);return 0
        path=initialize(a.profile,dict(schema_version=1,mode=a.mode,port=a.port,ib_host=a.ib_host,
            ib_port=a.ib_port,ib_client_id=a.client_id,ib_market_data_type=3,ib_timeout_ms=10000,
            data_dir=str(a.data_dir.expanduser()),sdk_root=str(a.sdk_root.expanduser())))
        print(f'Created {path}\nA private dashboard.token is stored beside the profile; it was not printed.\n'
              f'Launch: python3 tools/start_dashboard.py --profile {a.profile}\n'
              f'Reveal token locally: python3 tools/configure_dashboard.py --profile {a.profile} --show-token')
        return 0
    except (ValueError,OSError) as e:
        print('Setup stopped: '+str(e),file=sys.stderr);return 2
if __name__=='__main__': raise SystemExit(main())
