import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[2]/'tools'))
import local_config as c
import start_dashboard as launcher
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'config';self.env=patch.dict(os.environ,{'DTS_CONFIG_DIR':str(self.home)});self.env.start();self.addCleanup(self.env.stop)
        self.data={'schema_version':1,'mode':'research','port':8081,'ib_host':'172.26.112.1','ib_port':7497}
    def test_create_load_reuse(self):
        c.initialize('research',self.data);p,t=c.load('research');self.assertEqual(p,self.data);self.assertGreaterEqual(len(t),24)
        c.initialize('other',self.data);self.assertEqual(c.load('other')[1],t)
        self.assertEqual(stat.S_IMODE((self.home/'dashboard.token').stat().st_mode),0o600)
    def test_no_overwrite(self):
        c.initialize('research',self.data);saved=(self.home/'research.json').read_bytes()
        with self.assertRaises(ValueError):c.initialize('research',self.data)
        self.assertEqual((self.home/'research.json').read_bytes(),saved)
    def test_no_path_injection(self):
        for name in ['../other','x/y','', 'x'*100]:
            with self.assertRaises(ValueError):c.initialize(name,self.data)
    def test_shared_config_rejected(self):
        self.home.mkdir(mode=0o755)
        with self.assertRaises(ValueError):c.initialize('research',self.data)
    def test_symlink_token_rejected(self):
        self.home.mkdir(mode=0o700);outside=Path(self.tmp.name)/'outside';outside.write_text('x'*30)
        (self.home/'dashboard.token').symlink_to(outside)
        with self.assertRaises((OSError,ValueError)):c.initialize('research',self.data)
    def test_unknown_field_no_secret_in_profile(self):
        for item in [dict(self.data,password='no'),dict(self.data,port=True),dict(self.data,data_dir='relative')]:
            with self.assertRaises(ValueError):c.initialize('research',item)
    def test_profile_overrides_old_environment(self):
        c.initialize('research',self.data)
        with patch.dict(os.environ,{'DTS_API_TOKEN':'old','IB_HOST':'127.0.0.1'}):
            _,cmd,env=launcher.prepare(launcher.options(['--profile','research']))
        self.assertEqual(env['IB_HOST'],'172.26.112.1');self.assertEqual(env['DTS_API_TOKEN'],c.load('research')[1]);self.assertNotIn(env['DTS_API_TOKEN'],' '.join(cmd))
    def test_commandline_port_precedence(self):
        c.initialize('research',self.data)
        _,_,env=launcher.prepare(launcher.options(['--profile','research','--port','8082']))
        self.assertEqual(env['HTTP_PORT'],'8082')
    def test_no_implicit_profile_on_legacy_launch(self):
        c.initialize('research',self.data)
        with patch.dict(os.environ,{'DTS_API_TOKEN':''}):
            _,_,env=launcher.prepare(launcher.options([]))
        self.assertEqual(env['DTS_API_TOKEN'],'')
    def test_permissive_token_refused(self):
        c.initialize('research',self.data);(self.home/'dashboard.token').chmod(0o644)
        with self.assertRaises(ValueError):c.load('research')
if __name__=='__main__':unittest.main()
