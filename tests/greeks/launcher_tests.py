"""Launcher preflight only; no process, build, token display or Git mutation."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('launcher',Path(__file__).parents[2]/'tools/start_dashboard.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
class LauncherTests(unittest.TestCase):
    def test_research_overrides_inherited_broker_mode(self):
        with patch.dict(os.environ,{'DTS_BROKER':'tws','ENABLE_IB_WS':'true'}):
            _,cmd,env=launcher.prepare(launcher.options(['--dry-run']))
        self.assertEqual(env['DTS_BROKER'],'none');self.assertEqual(env['ENABLE_IB_WS'],'false')
        self.assertIn('-DDTS_WITH_IBKR=OFF',cmd)
    def test_bad_port_and_parallelism(self):
        for args in [['--port','80'],['--port','65536'],['--jobs','0'],['--jobs','100']]:
            with self.assertRaises(ValueError):launcher.prepare(launcher.options(args))
    def test_no_in_source_build(self):
        with self.assertRaises(ValueError):launcher.prepare(launcher.options(['--build-dir',str(launcher.ROOT)]))
    def test_other_checkout_cache_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'CMakeCache.txt').write_text('CMAKE_HOME_DIRECTORY:INTERNAL=/another/checkout\n')
            with self.assertRaises(ValueError):launcher.prepare(launcher.options(['--build-dir',tmp]))
            self.assertIn('/another/checkout',(Path(tmp)/'CMakeCache.txt').read_text())
    def test_native_requires_sdk_and_secret_not_on_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=launcher.options(['--mode','tws','--sdk-root',tmp])
            with self.assertRaises(ValueError):launcher.prepare(args)
            header=Path(tmp)/'source/cppclient/client/EClientSocket.h';header.parent.mkdir(parents=True);header.write_text('// fixture only')
            with patch.dict(os.environ,{'DTS_API_TOKEN':''}):
                with self.assertRaises(ValueError):launcher.prepare(args)
            token='local-test-token-not-a-secret-123'
            with patch.dict(os.environ,{'DTS_API_TOKEN':token}):
                _,cmd,env=launcher.prepare(args)
                self.assertNotIn(token,' '.join(cmd));self.assertEqual(env['DTS_API_TOKEN'],token)
if __name__=='__main__':unittest.main()
