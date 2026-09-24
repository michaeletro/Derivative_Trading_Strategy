"""Private local profiles. No credentials are written inside the repository."""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat

PROFILE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}\Z')
ALLOWED = {'schema_version', 'mode', 'port', 'sdk_root', 'data_dir', 'backup_dir',
           'ib_host', 'ib_port', 'ib_client_id', 'ib_market_data_type', 'ib_timeout_ms'}
ENV_KEYS = {'ib_host':'IB_HOST', 'ib_port':'IB_PORT', 'ib_client_id':'IB_CLIENT_ID',
            'ib_market_data_type':'IB_MARKET_DATA_TYPE', 'ib_timeout_ms':'IB_TIMEOUT_MS'}

def config_home() -> Path:
    value = os.environ.get('DTS_CONFIG_DIR')
    if value:
        p = Path(value).expanduser()
        if not p.is_absolute(): raise ValueError('DTS_CONFIG_DIR must be absolute')
        return p
    xdg = os.environ.get('XDG_CONFIG_HOME', '')
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home()/'.config')/'derivative-lab'

def private_dir(path: Path, create: bool = False) -> None:
    if create and not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077:
        raise ValueError('Config directory must be a real user-owned directory with mode 0700')

def read_private(path: Path, limit: int) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077 or s.st_nlink != 1:
            raise ValueError('Config/token must be private user-owned regular files, not links (mode 0600)')
        data = f.read(limit+1)
        if len(data)>limit: raise ValueError('Configuration file exceeds its limit')
    return data.decode('utf-8')

def write_new(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True); raise

def validate(data: dict) -> dict:
    if not isinstance(data, dict) or set(data)-ALLOWED or data.get('schema_version') != 1:
        raise ValueError('Unknown configuration schema or field')
    if data.get('mode') not in ('research','tws'): raise ValueError('Profile mode must be research or tws')
    for key,lo,hi in [('port',1024,65535),('ib_port',1,65535),('ib_client_id',1,2147483647),
                      ('ib_market_data_type',1,4),('ib_timeout_ms',100,60000)]:
        if key in data and (type(data[key]) is not int or not lo<=data[key]<=hi):
            raise ValueError('Invalid numeric profile field: '+key)
    host=data.get('ib_host','127.0.0.1')
    if not isinstance(host,str): raise ValueError('Invalid IB host')
    if host!='localhost':
        try: ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError as e: raise ValueError('IB host must be a numeric IPv4 address or localhost') from e
    for k in ('sdk_root','data_dir','backup_dir'):
        if k in data and (not isinstance(data[k],str) or not Path(data[k]).expanduser().is_absolute()):
            raise ValueError('Profile paths must be absolute: '+k)
    return data

def profile_paths(name: str):
    if not PROFILE.fullmatch(name): raise ValueError('Invalid profile name')
    home=config_home()
    resolved=home.expanduser().resolve()
    source_root=Path(__file__).resolve().parents[1]
    if resolved==source_root or source_root in resolved.parents or any((p/'.git').exists() for p in (resolved,*resolved.parents)):
        raise ValueError('Keep private configuration outside every Git checkout; use ~/.config/derivative-lab')
    return home,home/(name+'.json'),home/'dashboard.token'

def load(name: str):
    home,path,token_path=profile_paths(name); private_dir(home)
    try: data=validate(json.loads(read_private(path,16384)))
    except json.JSONDecodeError as e: raise ValueError('Invalid JSON profile') from e
    token=read_private(token_path,1024).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,256}',token): raise ValueError('Invalid saved dashboard token')
    return data,token

def initialize(name: str, data: dict) -> Path:
    data=validate(data); home,path,token_path=profile_paths(name); private_dir(home,True)
    # Never replace a profile or rotate an existing token implicitly.
    if path.exists() or path.is_symlink(): raise ValueError('Profile already exists; edit it explicitly rather than overwrite')
    if not token_path.exists() and not token_path.is_symlink():
        try: write_new(token_path,secrets.token_urlsafe(32)+'\n')
        except FileExistsError: pass  # Another explicit setup won the creation race.
    token=read_private(token_path,1024).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,256}',token): raise ValueError('Invalid existing token; not replaced')
    write_new(path,json.dumps(data,indent=2)+'\n')
    fd=os.open(home,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return path
