"""Execute the committed notebook with a clean synthetic-only environment."""
import argparse
import os
from pathlib import Path
import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[2]
p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
for key in ('DTS_LIQUIDITY_MANIFEST','DTS_ORDERBOOK_REPORT_DIR','DTS_DAILY_VARIANCE_PANEL'):
    os.environ.pop(key, None)
os.environ['DTS_REPO_ROOT'] = str(ROOT)
book = nbformat.read(ROOT/'research/liquidity_aware_hedging/notebooks/03_orderbook_model_lab.ipynb', as_version=4)
assert all(not c.get('outputs') and c.get('execution_count') is None for c in book.cells if c.cell_type=='code')
NotebookClient(book, timeout=180, kernel_name='python3', resources={'metadata':{'path':str(ROOT)}}).execute()
with a.output.open('x',encoding='utf-8') as stream:
    nbformat.write(book,stream)
print('Synthetic notebook executed; source notebook remains output-free.')
