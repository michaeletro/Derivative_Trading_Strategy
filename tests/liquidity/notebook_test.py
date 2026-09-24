"""Execute the synthetic learning notebook; never inherit private-data settings."""
import argparse
import os
from pathlib import Path
import tempfile
import nbformat
from nbclient import NotebookClient
ROOT=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--output',type=Path);args=p.parse_args()
os.environ.pop('DTS_LIQUIDITY_MANIFEST',None);os.environ.pop('DTS_DEPTH_EXPORT',None)
os.environ['DTS_REPO_ROOT']=str(ROOT)
nb=nbformat.read(ROOT/'research/liquidity_aware_hedging/notebooks/02_liquidity_prediction.ipynb',as_version=4)
assert not any(c.get('outputs') for c in nb.cells if c.cell_type=='code')
with tempfile.TemporaryDirectory() as tmp:
    NotebookClient(nb,timeout=120,kernel_name='python3',resources={'metadata':{'path':tmp}}).execute()
text='\n'.join(o.get('text','') for c in nb.cells for o in c.get('outputs',[]))
assert 'SYNTHETIC integration fixture' in text and 'PASS: repeatable' in text
assert not any(o.output_type=='error' for c in nb.cells for o in c.get('outputs',[]))
if args.output:
    args.output.parent.mkdir(parents=True,exist_ok=True);nbformat.write(nb,args.output)
print('Synthetic liquidity notebook executed; no observed-market or risk-improvement claim.')
