"""Execute the public synthetic notebook without broker/credential dependencies."""
import argparse
import os
from pathlib import Path
import tempfile
import nbformat
from nbclient import NotebookClient
ROOT=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--output',type=Path);args=p.parse_args()
# Deliberately exclude user data inputs; CI validates only the synthetic fixture.
os.environ.pop('DTS_DEPTH_EXPORT',None)
os.environ['DTS_REPO_ROOT']=str(ROOT)
notebook=nbformat.read(ROOT/'research/liquidity_aware_hedging/notebooks/01_data_and_cleaning.ipynb',as_version=4)
assert not any(c.get('outputs') for c in notebook.cells if c.cell_type=='code'), 'Source notebook must not contain private outputs'
with tempfile.TemporaryDirectory() as tmp:
    NotebookClient(notebook,timeout=120,kernel_name='python3',resources={'metadata':{'path':tmp}}).execute()
for cell in notebook.cells:
    for output in cell.get('outputs',[]):
        assert output.output_type!='error'
text='\n'.join(o.get('text','') for c in notebook.cells for o in c.get('outputs',[]))
assert 'SYNTHETIC protocol fixture' in text
if args.output:
    args.output.parent.mkdir(parents=True,exist_ok=True)
    nbformat.write(notebook,args.output)
print('Synthetic Phase I notebook executed successfully; no empirical result or credential used.')
