# AMS 518 / AMS 520 research foundation

Read `proposal.md`, `course_contributions.md`, `DATA_CARD.md`, `MODEL_ASSUMPTIONS.md`
and `AI_USAGE.md` before interpreting results. Current deliverable: a proposed
joint study, direct-depth acquisition/reconstruction, and Phase I data/cleaning.
The next offline learning stage is now available; CVaR optimization and LSV remain
subsequent deliverables. See the sections below for the distinction.

## Offline first: the notebook
From the repository root in an activated Python environment:

```bash
python3 -m pip install -r research/liquidity_aware_hedging/requirements.txt
python3 -m jupyterlab research/liquidity_aware_hedging/notebooks/01_data_and_cleaning.ipynb
```

Run all cells. With no `DTS_DEPTH_EXPORT` the notebook uses an explicitly SYNTHETIC
fixture. It needs no token, broker connection or running C++ server. The fixture
checks protocol/analysis behavior, not data access or empirical model performance.

For real data, first export a stopped session as documented in
`docs/course-depth.md`. Set `DTS_DEPTH_EXPORT` to its local JSON file before launching
Jupyter. The notebook reads only that file; no secret belongs in a cell. Start a
new kernel after changing the environment. Do not commit real-data outputs.
To keep executed private outputs outside the checkout, copy the notebook there
and set `DTS_REPO_ROOT` to the repository path before launching Jupyter.

CI runs `python3 tests/depth/notebook_test.py --output <temporary-path.ipynb>`.
Source notebooks have no saved outputs; temporary executed synthetic results can
be inspected separately. No instructor approval, teammates, real pilot session,
market calibration or observed-market predictive performance has been asserted.

## Next stage: future liquidity prediction

Read [the learning runbook](../../docs/liquidity-learning.md). The second notebook,
`notebooks/02_liquidity_prediction.ipynb`, constructs receipt-time targets and
compares persistence with train-only residual-ridge models on whole-date splits.
Its default is explicitly synthetic, not a market result. Real mode requires a
private manifest with compatible stopped exports and predeclared date partitions.

The [LaTeX paper](reports/theory_literature.tex) and [bibliography](reports/literature.bib)
develop the theory and critically review the literature. Compile from `reports/`
with `latexmk -pdf theory_literature.tex`. The paper's CVaR and LSV sections are
specifications for future work, not implemented/calibrated engines. Neither paper
nor synthetic notebook should be represented as completed empirical research.
