# Assumptions and claims register

| Item | Status and boundary |
|---|---|
| Recorded direct depth | Delivered provider rows; no full-book, order-ID or queue-fill guarantee |
| Phase I default data | Synthetic protocol fixture, not an empirical sample |
| Replay | Reconstructs local delivered-event order, not historical as-of exchange knowledge |
| Displayed crossing cost | Consumes visible rows against midpoint; no fill, latency, fees, hidden liquidity or impact model |
| Prediction | Planned 30-second future liquidity target; no trained model or validation result yet |
| CVaR decision rule | Proposed constrained scenario optimizer, not yet implemented |
| Existing hedge lab | Pre-existing synthetic GBM, fractional stock, symmetric financing and explicit assumed costs |
| LSV | Specified model and calibration work planned; not implemented in this foundation |
| Joint empirical/synthetic model | Not calibrated; do not attach unrelated books to paths and claim empirical dependence |
| Course submissions | Separate contributions proposed; shared-work approval not established |

Current book metrics use only the reconstructed prefix. Descriptive full-session
quality summaries may inspect the whole export and must not become historical
strategy inputs. A later time-grid sampler must define row age, epoch boundaries
and label availability; resampling a last-seen book is an assumption, not recovered
market observations. Trade-size-dependent ML costs must preserve convexity if an
LP/convex optimization claim is made. Fixed per-trade fees require separate treatment.
