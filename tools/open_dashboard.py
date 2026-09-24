#!/usr/bin/env python3
"""Open a local dashboard using a saved profile, without displaying its token."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
import webbrowser

from local_config import load as load_profile

CODE = re.compile(r'[a-f0-9]{64}')
NONCE = re.compile(r'[a-f0-9]{32}')


class LocalSigninError(Exception):
    """Safe messages only: never expose response bodies, headers or launch URLs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise LocalSigninError('Local sign-in refused an HTTP redirect.')


def validate_port(port: int) -> int:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LocalSigninError('Dashboard port must be between 1024 and 65535.')
    return port


def local_request(port: int, route: str, token: str | None = None):
    validate_port(port)
    if route not in ('/api/auth/status', '/api/auth/launch'):
        raise LocalSigninError('Unsupported local sign-in endpoint.')
    headers = {'Accept': 'application/json'}
    if token is not None:
        if not re.fullmatch(r'[A-Za-z0-9_-]{24,256}', token):
            raise LocalSigninError('The saved local token has an invalid format.')
        headers['Authorization'] = 'Bearer ' + token
    data = b'{}' if route == '/api/auth/launch' else None
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = Request(f'http://127.0.0.1:{port}{route}', data=data, headers=headers)
    # Never send the profile token to a proxy or redirect destination.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                raise LocalSigninError('Local sign-in returned an unexpected HTTP status.')
            payload = response.read(4097)
            if len(payload) > 4096:
                raise LocalSigninError('Local sign-in response was too large.')
    except HTTPError as error:
        if error.code in (401, 403):
            raise LocalSigninError('The running server does not accept this profile. Restart it with the same --profile.') from None
        if error.code == 404:
            raise LocalSigninError('This server does not support auto sign-in. Update and restart it first.') from None
        raise LocalSigninError('Local sign-in request failed; no browser was opened.') from None
    except (URLError, TimeoutError, OSError):
        raise LocalSigninError('Local dashboard is not reachable; start it with your saved profile first.') from None
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError):
        raise LocalSigninError('Local sign-in response was not valid JSON.') from None
    if not isinstance(result, dict) or type(result.get('schema_version')) is not int or result['schema_version'] != 1:
        raise LocalSigninError('Local sign-in response is incompatible.')
    return result


def launch_url(port: int, code: str) -> str:
    validate_port(port)
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise LocalSigninError('Invalid one-use launch code.')
    return f'http://127.0.0.1:{port}/#local-signin={code}'


def open_browser(port: int, code: str) -> None:
    url = launch_url(port, code)
    if os.environ.get('WSL_DISTRO_NAME') or os.environ.get('WSL_INTEROP'):
        powershell = shutil.which('powershell.exe')
        if not powershell:
            candidate = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
            if candidate.is_file():
                powershell = str(candidate)
        if not powershell:
            raise LocalSigninError('Windows browser opener unavailable. Enable WSL Windows interop or use manual token entry.')
        try:
            # URL arrives via stdin, not shell interpolation or command arguments.
            subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command',
                            '$u=[Console]::In.ReadToEnd(); Start-Process -FilePath $u'],
                           input=url, text=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            raise LocalSigninError('Windows could not open its browser. No saved token was displayed.') from None
    else:
        try:
            opened = webbrowser.open(url, new=2)
        except Exception:
            raise LocalSigninError('The local browser could not be opened.') from None
        if not opened:
            raise LocalSigninError('No graphical browser opener is available. Use manual token entry on this system.')


def open_profile(name: str, port: int | None = None) -> None:
    profile, token = load_profile(name)
    port = validate_port(port if port is not None else profile.get('port', 8081))
    result = local_request(port, '/api/auth/launch', token)
    code = result.get('code')
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise LocalSigninError('The local sign-in code was invalid.')
    open_browser(port, code)


def wait_and_open(plan: dict) -> None:
    if not isinstance(plan, dict) or set(plan) != {'port', 'code', 'nonce', 'parent_pid'}:
        raise LocalSigninError('Invalid local launcher message.')
    port = validate_port(plan['port'])
    launch_url(port, plan['code'])
    if not isinstance(plan['nonce'], str) or not NONCE.fullmatch(plan['nonce']):
        raise LocalSigninError('Invalid local launcher identity.')
    if type(plan['parent_pid']) is not int or plan['parent_pid'] < 1:
        raise LocalSigninError('Invalid launcher process identity.')
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if os.getppid() != plan['parent_pid']:
            raise LocalSigninError('Server exited before automatic sign-in; no browser was opened.')
        try:
            state = local_request(port, '/api/auth/status')
        except LocalSigninError:
            time.sleep(.25)
            continue
        if state.get('launch_nonce') != plan['nonce']:
            raise LocalSigninError('A different server is listening; automatic sign-in was cancelled.')
        open_browser(port, plan['code'])
        return
    raise LocalSigninError('Server startup timed out. Reopen it later with tools/open_dashboard.py --profile NAME.')


def start_helper(port: int, code: str, nonce: str) -> None:
    # No saved token in helper argv/environment/stdin. Its sole credential is a
    # fresh one-use code; the parent seeds the same code in the new server.
    keep = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR',
            'DBUS_SESSION_BUS_ADDRESS', 'WSL_DISTRO_NAME', 'WSL_INTEROP', 'SYSTEMROOT', 'SystemRoot')
    clean_env = {key: os.environ[key] for key in keep if key in os.environ}
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--launch-stdin'],
                             stdin=subprocess.PIPE, text=True, env=clean_env, start_new_session=True)
    assert child.stdin is not None
    child.stdin.write(json.dumps(dict(port=port, code=code, nonce=nonce, parent_pid=os.getpid())))
    child.stdin.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', help='Saved profile used by the running server')
    parser.add_argument('--port', type=int, help='Explicit dashboard port override')
    parser.add_argument('--launch-stdin', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.launch_stdin:
            payload = sys.stdin.read(2049)
            if len(payload) > 2048:
                raise LocalSigninError('Invalid local launcher message.')
            wait_and_open(json.loads(payload))
        else:
            if not args.profile:
                raise LocalSigninError('Supply the saved profile with --profile NAME.')
            open_profile(args.profile, args.port)
        print('Opened local dashboard for automatic sign-in. No saved token was displayed.', flush=True)
        return 0
    except LocalSigninError as error:
        print('Auto sign-in stopped: ' + str(error), file=sys.stderr)
    except (ValueError, OSError):
        print('Auto sign-in stopped: saved profile/token could not be read safely. Check local configuration and permissions.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
