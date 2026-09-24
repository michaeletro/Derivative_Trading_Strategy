"""Local opener tests never launch a real browser or use real profile credentials."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
import open_dashboard as opener
import local_config
import start_dashboard

class HelperTests(unittest.TestCase):
    def test_url_only_loopback_and_one_use_code(self):
        code=secrets.token_hex(32);url=opener.launch_url(8081,code)
        self.assertEqual(url,'http://127.0.0.1:8081/#local-signin='+code)
        for value in ['bad','a'*63,'a'*64+'&token=abc']:
            with self.assertRaises(opener.LocalSigninError):opener.launch_url(8081,value)
        for port in [80,True,65536]:
            with self.assertRaises(opener.LocalSigninError):opener.launch_url(port,code)
    def test_profile_credential_sent_only_to_local_launch_endpoint(self):
        token=secrets.token_urlsafe(32);code=secrets.token_hex(32)
        with patch.object(opener,'load_profile',return_value=({'port':8082},token)),patch.object(opener,'local_request',return_value={'code':code}) as call,patch.object(opener,'open_browser') as browser:
            opener.open_profile('fixture');call.assert_called_once_with(8082,'/api/auth/launch',token);browser.assert_called_once_with(8082,code)
    def test_cli_does_not_print_saved_token_or_ticket(self):
        token=secrets.token_urlsafe(32);code=secrets.token_hex(32);out=io.StringIO()
        with patch.object(opener,'load_profile',return_value=({},token)),patch.object(opener,'local_request',return_value={'code':code}),patch.object(opener,'open_browser'),contextlib.redirect_stdout(out):
            self.assertEqual(opener.main(['--profile','fixture']),0)
        self.assertNotIn(token,out.getvalue());self.assertNotIn(code,out.getvalue())
    def test_wrong_server_nonce_never_opens_browser(self):
        plan={'port':8081,'code':secrets.token_hex(32),'nonce':secrets.token_hex(16),'parent_pid':os.getppid()}
        with patch.object(opener,'local_request',return_value={'schema_version':1,'launch_nonce':'other'}),patch.object(opener,'open_browser') as browser:
            with self.assertRaises(opener.LocalSigninError):opener.wait_and_open(plan)
            browser.assert_not_called()
    def test_correct_server_opens_once(self):
        plan={'port':8081,'code':secrets.token_hex(32),'nonce':secrets.token_hex(16),'parent_pid':os.getppid()}
        with patch.object(opener,'local_request',return_value={'schema_version':1,'launch_nonce':plan['nonce']}),patch.object(opener,'open_browser') as browser:
            opener.wait_and_open(plan);browser.assert_called_once_with(8081,plan['code'])
    def test_dead_parent_stops_helper(self):
        plan={'port':8081,'code':secrets.token_hex(32),'nonce':secrets.token_hex(16),'parent_pid':99999999}
        with self.assertRaises(opener.LocalSigninError):opener.wait_and_open(plan)
    def test_windows_handoff_uses_stdin_not_secret_arguments(self):
        code=secrets.token_hex(32)
        with patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(opener.shutil,'which',return_value='/mnt/c/powershell.exe'),patch.object(opener.subprocess,'run') as run:
            opener.open_browser(8081,code)
        args,kw=run.call_args;self.assertNotIn(code,' '.join(args[0]));self.assertIn(code,kw['input']);self.assertFalse(kw.get('shell',False))
    def test_helper_does_not_inherit_auth_environment(self):
        child=MagicMock();child.stdin=io.StringIO();written=[]
        child.stdin=MagicMock();child.stdin.write.side_effect=written.append
        with patch.dict(os.environ,{'DTS_API_TOKEN':'must-not-leave-parent','OTHER_SECRET':'also-secret'}),patch.object(opener.subprocess,'Popen',return_value=child) as spawn:
            opener.start_helper(8081,'a'*64,'b'*32)
        args,kw=spawn.call_args
        self.assertNotIn('DTS_API_TOKEN',kw['env']);self.assertNotIn('OTHER_SECRET',kw['env']);self.assertNotIn('a'*64,' '.join(args[0]));self.assertNotIn('must-not-leave-parent',''.join(written))
    def test_repository_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'DTS_CONFIG_DIR':tmp+'/repo/config'}):
            Path(tmp,'repo/.git').mkdir(parents=True)
            with self.assertRaises(ValueError):local_config.profile_paths('fixture')
    def test_manual_and_profile_launch_flags(self):
        self.assertIsNone(start_dashboard.options(['--profile','fixture']).open_browser)
        self.assertFalse(start_dashboard.options(['--no-open-browser']).open_browser)
        self.assertTrue(start_dashboard.options(['--open-browser']).open_browser)
    def test_redirect_not_followed_and_proxy_ignored(self):
        calls=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path);self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/stolen');self.end_headers()
            def do_GET(self):calls.append(self.path);self.send_response(500);self.end_headers()
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:1','http_proxy':'http://127.0.0.1:1','NO_PROXY':'','no_proxy':''}):
                with self.assertRaises(opener.LocalSigninError):opener.local_request(server.server_port,'/api/auth/launch',secrets.token_urlsafe(32))
            self.assertEqual(calls,['/api/auth/launch'])
        finally:server.shutdown();server.server_close();thread.join()
    def test_saved_profile_read_error_is_sanitized(self):
        err=io.StringIO()
        with patch.object(opener,'load_profile',side_effect=ValueError('sensitive content')),contextlib.redirect_stderr(err):
            self.assertEqual(opener.main(['--profile','fixture']),2)
        self.assertNotIn('sensitive content',err.getvalue())

if __name__=='__main__':unittest.main()
