"""Authenticated HTTP lifecycle using clearly synthetic, isolated tick fixtures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_helpers',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)

def main(exe,seed):
    checks=0
    def ck(value):
        nonlocal checks
        assert value
        checks+=1
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        ident=subprocess.run([str(Path(seed).resolve()),str(root/'data')],capture_output=True,text=True,check=True).stdout.strip()
        path='/api/ticks/downloads/'+ident
        with helpers.Server(exe,root) as s:
            for endpoint,body in [('/api/ticks/downloads',None),('/api/ticks/downloads',{}),(path,None),(path+'/view',{'after':0,'limit':1000}),(path+'/cancel',{}),(path+'/resume',{})]:
                ck(s.call(endpoint,body,auth=False)[0]==401)
                ck(s.call(endpoint,body,Origin='https://untrusted.invalid')[0]==403)
            ck(s.call('/api/ticks/downloads',{})[0]==409)
            ck(s.call('/api/ticks/downloads',{'unknown':1})[0]==400)
            ck(s.call(path+'/resume',{})[0]==409)
            ck(s.call(path+'/cancel',{'unknown':1})[0]==400)
            ck(s.call('/api/ticks/downloads')[1]['downloads'][0]['download_id']==ident)
            status=s.call(path)[1];ck(status['state']=='complete' and status['tick_count']==1007 and not status['active'])
            for body in [{'after':-1,'limit':1},{'after':0,'limit':1001},{'after':0.5,'limit':1},{'after':0,'limit':1,'unknown':1}]:
                ck(s.call(path+'/view',body)[0]==400)
            code,first=s.call(path+'/view',{'after':0,'limit':1000})
            ck(code==200 and len(first['ticks'])==1000 and first['has_more'])
            second=s.call(path+'/view',{'after':1000,'limit':1000})[1]
            ck(len(second['ticks'])==7 and not second['has_more'] and second['ticks'][-1]['ordinal']==1007)
            ck(all(t['time_s']<status['end_s'] for t in first['ticks']+second['ticks']))
            for asset in ['ticks.mjs','ticks-model.mjs']:
                with urlopen(s.origin+'/dashboard/'+asset) as r:ck(r.status==200 and len(r.read())>100)
            ck(s.stop()==0)
        with helpers.Server(exe,root) as s:
            ck(s.call(path+'/view',{'after':1000,'limit':1000})[1]==second)
            ck(s.call(path+'/cancel',{})[1]['state']=='complete')
            directory=root/'data/historical-ticks'/ident
            meta=json.loads((directory/'state.json').read_text())
            with (directory/meta['pages'][0]['file']).open('a') as f:f.write(' ')
            ck(s.call(path+'/view',{'after':0,'limit':1})[0]==503)
    print(f'{checks} tick HTTP authorization, paging, bounds, restart and corruption checks passed')
if __name__=='__main__':main(*sys.argv[1:])
